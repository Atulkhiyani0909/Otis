import express, { Request, Response } from "express";
import axios from "axios";
import dotenv from "dotenv";
import crypto from "crypto";
import FormData from "form-data";
import { createParser, type EventSourceMessage } from "eventsource-parser";
import { saveUserTokens, getUserTokens } from "./tokenStore";
import { getAuthUrl, oauth2Client } from "./googleAuth";
import fs from "fs";
import path from "path";
import cron from "node-cron";

dotenv.config();

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || "";
const TELEGRAM_API_BASE = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}`;

// Dynamic agent URLs
const PYTHON_AGENT_URL = process.env.PYTHON_AGENT_URL || "http://127.0.0.1:8000/api/agent/dispatch";
const PYTHON_BASE_URL = PYTHON_AGENT_URL.replace(/\/api\/agent\/dispatch.*$/, "");
const PYTHON_CONFIRM_URL = `${PYTHON_BASE_URL}/api/agent/confirm-action`;

const TOKENS_PATH = path.resolve(__dirname, "../tokens.json");
const POLLS_PATH = path.resolve(__dirname, "../polls.json");

// Optional: public URL of this gateway, e.g. https://xyz.ngrok.app
// If set, the webhook is registered on startup with poll_answer updates enabled.
const WEBHOOK_URL = process.env.WEBHOOK_URL || "";

// ---- Poll behaviour ----
// Forward poll votes to the agent so it can act on them (set to "false" to disable)
const FORWARD_POLL_ANSWERS_TO_AGENT = process.env.FORWARD_POLL_ANSWERS_TO_AGENT !== "false";
// Wait this long after the LAST tap before acting (so changed votes / multi-select are settled)
const POLL_DEBOUNCE_MS = Number(process.env.POLL_DEBOUNCE_MS || 3000);
// Close single-choice polls after you answer so the decision can't change afterwards
const POLL_LOCK_AFTER_ANSWER = process.env.POLL_LOCK_AFTER_ANSWER !== "false";
const POLL_TTL_MS = 48 * 60 * 60 * 1000;

// ---- Proactive behaviour (the bot messages you first) ----
const TIMEZONE = process.env.TIMEZONE || "Asia/Kolkata";
const PROACTIVE_ENABLED = process.env.PROACTIVE_ENABLED !== "false";
const MORNING_CRON = process.env.MORNING_CRON || "0 8 * * 1-5"; // 08:00 Mon-Fri
const HEARTBEAT_CRON = process.env.HEARTBEAT_CRON || "*/30 9-19 * * 1-6"; // every 30 min, 09-19, Mon-Sat
const WRAPUP_CRON = process.env.WRAPUP_CRON || "30 18 * * 1-5"; // 18:30 Mon-Fri
const NO_ALERT_TOKEN = "[NO_ALERT]";

// Staged actions storage
const pendingActions = new Map<string, { chatId: string | number; actionData: any; tokens: any }>();

// ---------------------------------------------------------------------------
// Per-chat queue: one agent run at a time per chat
// (stops a scheduled check-in, a button tap and a poll vote from colliding)
// ---------------------------------------------------------------------------

const chatQueues = new Map<string, Promise<void>>();

function runExclusive(chatId: string | number, fn: () => Promise<void>): Promise<void> {
  const key = String(chatId);
  const previous = chatQueues.get(key) || Promise.resolve();
  const next = previous.catch(() => {}).then(fn);
  chatQueues.set(key, next);
  const cleanup = () => {
    if (chatQueues.get(key) === next) chatQueues.delete(key);
  };
  next.then(cleanup, cleanup);
  return next;
}

// ---------------------------------------------------------------------------
// Tool Menu
// ---------------------------------------------------------------------------
//
// Items WITHOUT `ask` run immediately when tapped.
// Items WITH `ask` first ask you for details ("Which task is done?"), then your
// next text message is inserted into {input} in the prompt and sent to the agent.

interface MenuItem {
  label: string;
  prompt: string;
  ask?: string;
}

const ITEMS: Record<string, MenuItem> = {
  // ---- One-tap summaries ----
  imp: {
    label: "🔥 Imp Today",
    prompt:
      "Check my calendar, unread priority emails, and pending tasks. Give me ONLY the top 5 most important things I must handle today, ranked by urgency.",
  },
  check: {
    label: "🔔 Check Now",
    prompt:
      "[AUTOMATED:heartbeat] On-demand check. In parallel: (1) calendar events in the next 2 hours, (2) unread emails that look important or need a reply, (3) tasks overdue or due today. Reply with a SHORT summary of what needs my attention (max 8 lines). If nothing needs attention, reply in one line: All clear ✅. If I need to choose how to handle something, end with ONE poll.",
  },
    // ---- Reminders ----
  set_reminder: {
    label: "⏰ Set Reminder",
    ask: "What should I remind you about, and when? (e.g. call Rahul tomorrow at 5 PM)",
    prompt: "Set a reminder: {input}.",
  },
  set_recurring: {
    label: "🔁 Repeating Task",
    ask: "What should repeat, and when? (e.g. every weekday 9 AM tell me my unread emails)",
    prompt:
      "Set a recurring reminder or scheduled task: {input}. If I want you to fetch or check something at that time, use run_agent=true.",
  },
  imp_mail: {
    label: "⭐ Important Mails",
    prompt:
      "Call your Gmail tool and list my important/starred/priority emails from the last 3 days. For each: sender, subject, 1-line summary, and whether it needs a reply.",
  },
  unread_mail: {
    label: "📬 Unread Mails",
    prompt:
      "Call your Gmail tool and list my unread emails (newest first, max 10). For each: sender, subject, 1-line summary.",
  },
  tasks: {
    label: "✅ Pending Tasks",
    prompt:
      "Call your Google Tasks tool and list all my pending tasks, grouped by overdue, due today, and upcoming.",
  },
  calendar: {
    label: "📅 Today's Schedule",
    prompt: "Call your calendar tool and list today's events with times and locations.",
  },

  // ---- Gmail & Contacts ----
  search_mail: {
    label: "🔎 Search Mail",
    ask: "What should I search for? (sender, subject or keywords)",
    prompt:
      "Search my email for: {input}. List the top 5 matching threads with sender, subject, date and a 1-line summary.",
  },
  compose_mail: {
    label: "✉️ Compose Email",
    ask: "Who is it for and what should it say? (e.g. Rahul - tell him the meeting moved to 4 PM)",
    prompt:
      "Draft an email for me: {input}. If I only gave a name, look up the email address with your contacts tool first. Then stage it with send_email so I can approve it.",
  },
  find_contact: {
    label: "👥 Find Contact",
    ask: "Whose contact details do you need? (name)",
    prompt: "Look up the contact details (email, phone) for: {input}.",
  },

  // ---- Calendar ----
  week: {
    label: "🗓️ Next 7 Days",
    prompt: "List my calendar events for the next 7 days, grouped by day, with times.",
  },
  new_event: {
    label: "➕ New Event",
    ask: "Describe the event (title, day, time, duration)",
    prompt: "Create a calendar event: {input}.",
  },
  update_event: {
    label: "✏️ Reschedule Event",
    ask: "Which event and what should change? (e.g. Team sync to 5 PM Friday)",
    prompt:
      "Find the matching event on my calendar and update it: {input}. If several events match, ask me to pick one.",
  },
  delete_event: {
    label: "🗑️ Cancel Event",
    ask: "Which event should I cancel?",
    prompt:
      "Find the matching calendar event for: {input}. Show me which event you found and ask me to confirm before deleting it.",
  },

  // ---- Tasks ----
  add_task: {
    label: "➕ Add Task",
    ask: "What's the task? (add a due date if it has one)",
    prompt: "Add this to my Google Tasks: {input}.",
  },
  complete_task: {
    label: "✔️ Complete Task",
    ask: "Which task is done?",
    prompt: "List my tasks, find the one matching: {input}, and mark it complete. If several match, ask me to pick one.",
  },
  delete_task: {
    label: "🗑️ Delete Task",
    ask: "Which task should I delete?",
    prompt:
      "List my tasks and find the one matching: {input}. Show me which one you found and ask me to confirm before deleting it.",
  },

  // ---- Drive & Sheets ----
  search_drive: {
    label: "📁 Search Drive",
    ask: "What file are you looking for? (name or keywords)",
    prompt: "Search my Google Drive for: {input}. List the top matches with name, type and last modified date.",
  },
  read_file: {
    label: "📄 Read File",
    ask: "Which file should I open? (name or keywords)",
    prompt: "Find this file in my Google Drive: {input}. Read its content and give me a concise summary with the key points.",
  },
  read_sheet: {
    label: "📊 Read Sheet",
    ask: "Which spreadsheet (and tab/range, if needed)?",
    prompt: "Find and read this Google Sheet: {input}. Show me the data in a short, readable summary.",
  },
  add_row: {
    label: "📝 Add Row to Sheet",
    ask: "Which sheet, and what values should go in the new row?",
    prompt: "Append a new row to a Google Sheet: {input}.",
  },
  new_sheet: {
    label: "🆕 New Spreadsheet",
    ask: "What should it be called, and what columns should it have?",
    prompt: "Create a new Google Sheet: {input}.",
  },
  clear_range: {
    label: "🧹 Clear Range",
    ask: "Which sheet and which range should be cleared?",
    prompt:
      "I want to clear a range in a Google Sheet: {input}. Show me exactly what will be cleared and ask me to confirm before doing it.",
  },
  delete_row: {
    label: "🗑️ Delete Sheet Row",
    ask: "Which sheet and which row should be deleted?",
    prompt:
      "I want to delete a row in a Google Sheet: {input}. Show me the row you found and ask me to confirm before deleting it.",
  },

  // ---- Web, Media & YouTube ----
  web_search: {
    label: "🔍 Web Search",
    ask: "What should I search for?",
    prompt: "Search the web for: {input}. Give me a short, accurate summary with the key facts.",
  },
  read_url: {
    label: "🌐 Read Webpage",
    ask: "Send the webpage link",
    prompt: "Open this webpage and summarize it with the key points: {input}",
  },
  find_image: {
    label: "📸 Find Image",
    ask: "What image should I find?",
    prompt: "Search for an image of: {input}.",
  },
  gen_image: {
    label: "🎨 Generate Image",
    ask: "Describe the image you want",
    prompt: "Generate an image: {input}.",
  },
  yt_transcript: {
    label: "🎥 YouTube Summary",
    ask: "Send the YouTube link",
    prompt: "Get the transcript of this YouTube video and give me a concise summary with the key points: {input}",
  },

  // ---- Weather & Travel ----
  weather: {
    label: "⛅ Weather",
    ask: "Which city? (or share your location pin instead)",
    prompt: "What's the current weather in {input}? Keep it short.",
  },
  commute: {
    label: "🚗 Commute & Distance",
    ask: "From where to where? (e.g. Bhopal Station to DB Mall)",
    prompt: "Calculate the commute time and distance for: {input}.",
  },
};

interface MenuCategory {
  label: string;
  keys: string[];
}

const CATEGORIES: Record<string, MenuCategory> = {
  gmail: { label: "📧 Gmail & Contacts", keys: ["unread_mail", "imp_mail", "search_mail", "compose_mail", "find_contact"] },
  calendar: { label: "📅 Calendar", keys: ["calendar", "week", "new_event", "update_event", "delete_event"] },
  tasks: { label: "✅ Tasks", keys: ["tasks", "add_task", "complete_task", "delete_task"] },
  drive: {
    label: "📁 Drive & Sheets",
    keys: ["search_drive", "read_file", "read_sheet", "add_row", "new_sheet", "clear_range", "delete_row"],
  },
    reminders: { label: "⏰ Reminders", keys: ["set_reminder", "set_recurring"] },
  web: { label: "🌐 Web & Media", keys: ["web_search", "read_url", "find_image", "gen_image", "yt_transcript"] },
  travel: { label: "⛅ Weather & Travel", keys: ["weather", "commute"] },
};

// Items shown at the top of the main menu
const TOP_KEYS = ["imp", "check", "imp_mail", "unread_mail", "tasks", "calendar"];

function chunk<T>(arr: T[], size = 2): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

function itemButton(key: string) {
  return { text: ITEMS[key].label, callback_data: `qa:${key}` };
}

// Full menu: top shortcuts + categories
function buildMainMenu() {
  const rows: any[][] = [];
  rows.push(...chunk(TOP_KEYS.map(itemButton), 2));
  rows.push(
    ...chunk(
      Object.entries(CATEGORIES).map(([id, c]) => ({ text: c.label, callback_data: `menu:${id}` })),
      2
    )
  );
  rows.push([{ text: "✖ Close", callback_data: "menu:compact" }]);
  return { inline_keyboard: rows };
}

// One category's tools + Back button
function buildCategoryMenu(categoryId: string) {
  const cat = CATEGORIES[categoryId];
  const rows: any[][] = chunk(cat.keys.map(itemButton), 2);
    if (categoryId === "reminders") rows.push([{ text: "📋 My Reminders", callback_data: "rem:list" }]);
  rows.push([{ text: "⬅️ Back", callback_data: "menu:main" }]);
  return { inline_keyboard: rows };
}

// Small keyboard attached under normal replies
function buildCompactMenu() {
  return {
    inline_keyboard: [
      [
        { text: "📬 Unread", callback_data: "qa:unread_mail" },
        { text: "📅 Today", callback_data: "qa:calendar" },
        { text: "✅ Tasks", callback_data: "qa:tasks" },
      ],
      [{ text: "📋 All Tools", callback_data: "menu:main" }],
    ],
  };
}

async function sendQuickMenu(chatId: string | number, text = "What would you like to do?"): Promise<void> {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
      chat_id: chatId,
      text,
      reply_markup: buildMainMenu(),
    });
  } catch (err: any) {
    console.error("Failed to send quick menu:", err.response?.data || err.message);
  }
}

// Tools that need details: chatId -> which tool is waiting for the user's next message
const pendingInputs = new Map<string, { key: string; expires: number }>();
const INPUT_TTL_MS = 10 * 60 * 1000;

// ---------------------------------------------------------------------------
// Polls (the bot creates polls on its own; you only answer them)
// ---------------------------------------------------------------------------

interface PollSpec {
  question: string;
  options: string[];
  multiple?: boolean;
  correct_index?: number; // set => Telegram quiz poll with one right answer (0-based)
  explanation?: string; // shown by Telegram after answering a quiz (max 200 chars)
}

// poll_answer updates don't include chat_id, so every poll we send is remembered.
// It is saved to polls.json so votes still work after a restart.
interface TrackedPoll {
  chatId: string | number;
  question: string;
  options: string[];
  multiple: boolean;
  correctIndex?: number; // present only for quiz polls
  messageId?: number;
  createdAt: number;
}

function loadPolls(): Map<string, TrackedPoll> {
  const map = new Map<string, TrackedPoll>();
  try {
    const raw = JSON.parse(fs.readFileSync(POLLS_PATH, "utf-8")) as Record<string, TrackedPoll>;
    const now = Date.now();
    for (const [id, p] of Object.entries(raw)) {
      if (p && now - (p.createdAt || 0) < POLL_TTL_MS) map.set(id, p);
    }
  } catch {
    // no file yet / unreadable: start empty
  }
  return map;
}

const pendingPolls = loadPolls();

function savePolls(): void {
  try {
    fs.writeFileSync(POLLS_PATH, JSON.stringify(Object.fromEntries(pendingPolls)));
  } catch (err: any) {
    console.error("[POLLS] Could not save polls.json:", err.message);
  }
}

// Drop expired polls once an hour
setInterval(() => {
  const now = Date.now();
  let changed = false;
  for (const [id, p] of pendingPolls) {
    if (now - p.createdAt >= POLL_TTL_MS) {
      pendingPolls.delete(id);
      changed = true;
    }
  }
  if (changed) savePolls();
}, 60 * 60 * 1000);

// pollId -> timer for the "wait until the user stops tapping" debounce
const pollTimers = new Map<string, NodeJS.Timeout>();

async function sendTelegramPoll(chatId: string | number, spec: PollSpec): Promise<void> {
  const question = String(spec.question || "").slice(0, 300);
  const options = (spec.options || []).slice(0, 10).map((o) => String(o).slice(0, 100));

  if (!question || options.length < 2) {
    await sendTelegramMessage(chatId, "⚠️ A poll needs a question and at least 2 options.");
    return;
  }

  try {
    const isQuiz =
      Number.isInteger(spec.correct_index) &&
      (spec.correct_index as number) >= 0 &&
      (spec.correct_index as number) < options.length;

    const res = await axios.post(`${TELEGRAM_API_BASE}/sendPoll`, {
      chat_id: chatId,
      question,
      options,
      is_anonymous: false, // REQUIRED, otherwise Telegram won't send poll_answer updates
      ...(isQuiz
        ? {
            type: "quiz",
            correct_option_id: spec.correct_index,
            explanation: spec.explanation ? String(spec.explanation).slice(0, 200) : undefined,
          }
        : { allows_multiple_answers: Boolean(spec.multiple) }),
    });

    const pollId = res.data?.result?.poll?.id;
    if (pollId) {
      pendingPolls.set(pollId, {
        chatId,
        question,
        options,
        multiple: !isQuiz && Boolean(spec.multiple),
        correctIndex: isQuiz ? (spec.correct_index as number) : undefined,
        messageId: res.data?.result?.message_id,
        createdAt: Date.now(),
      });
      savePolls();
    }
  } catch (err: any) {
    console.error("Failed to send poll:", err.response?.data || err.message);
    await sendTelegramMessage(chatId, "⚠️ Couldn't create the poll.");
  }
}

async function stopTelegramPoll(chatId: string | number, messageId: number): Promise<void> {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/stopPoll`, { chat_id: chatId, message_id: messageId });
  } catch (err: any) {
    console.error("Failed to stop poll:", err.response?.data || err.message);
  }
}

// Runs once the user has stopped tapping for POLL_DEBOUNCE_MS
async function handlePollVote(pollId: string, optionIds: number[]): Promise<void> {
  const ctx = pendingPolls.get(pollId);
  if (!ctx) return;

  const chosen = optionIds.map((i) => ctx.options[i]).filter(Boolean);

  // Empty = the user retracted their vote
  if (chosen.length === 0) {
    await sendTelegramMessage(ctx.chatId, "🗳️ Vote retracted. No action taken.");
    return;
  }

  // Quiz polls are graded here; normal polls just acknowledge the choice
  const isQuiz = ctx.correctIndex !== undefined;
  const quizCorrect = isQuiz && optionIds[0] === ctx.correctIndex;
  const correctText = isQuiz ? ctx.options[ctx.correctIndex as number] : "";

  if (isQuiz) {
    await sendTelegramMessage(
      ctx.chatId,
      quizCorrect ? "✅ *Correct!*" : `❌ *Not quite.* Correct answer: *${correctText}*`
    );
    // Telegram already locks a quiz after the first answer
    pendingPolls.delete(pollId);
    savePolls();
  } else {
    await sendTelegramMessage(ctx.chatId, `🗳️ Got it! You picked: *${chosen.join(", ")}*`);
  }

  // Single-choice decision polls are closed after the answer so they can't change later
  if (!isQuiz && POLL_LOCK_AFTER_ANSWER && !ctx.multiple) {
    if (ctx.messageId) await stopTelegramPoll(ctx.chatId, ctx.messageId);
    pendingPolls.delete(pollId);
    savePolls();
  }

  if (!FORWARD_POLL_ANSWERS_TO_AGENT) return;

  const pollTokens = getUserTokens(String(ctx.chatId)) || {};
  const pollHasTokens = Boolean(pollTokens && (pollTokens.access_token || pollTokens.refresh_token));
  if (!pollHasTokens) return;

  await processUserPrompt({
    chatId: ctx.chatId,
    promptText: isQuiz
      ? `Quiz answer: for your question "${ctx.question}" I chose "${chosen[0]}", which was ` +
        (quizCorrect ? "CORRECT. " : `WRONG (the correct answer was "${correctText}"). `) +
        "Keep my running score. If the quiz has more questions, send the next one now as a quiz poll (one question only). " +
        "If that was the last question, give my final score out of the total and a one-line tip. " +
        "Do not repeat the explanation I already saw."
      : `I answered your poll "${ctx.question}" (options: ${ctx.options.join(" | ")}) with: ${chosen.join(", ")}. ` +
        "Continue the task from where you stopped, using this choice. Do not repeat the poll. " +
        "If no further action is needed, confirm in one short line.",
    tokens: pollTokens,
  });
}

// Agent can emit: [POLL: {"question":"...","options":["a","b"],"multiple":false}]

function extractPollFromReply(reply: string): { poll: PollSpec | null; cleaned: string } {
  const match = reply.match(/\[POLL:\s*(\{[\s\S]*?\})\s*\]/);
  if (!match) return { poll: null, cleaned: reply };
  try {
    const poll = JSON.parse(match[1]) as PollSpec;
    return { poll, cleaned: reply.replace(match[0], "").trim() };
  } catch {
    return { poll: null, cleaned: reply.replace(match[0], "").trim() };
  }
}



// ---------------------------------------------------------------------------
// Reminders, alarms & scheduled tasks
// ---------------------------------------------------------------------------
// One-time reminders are checked by a 15s ticker (survives restarts, no setTimeout
// overflow). Recurring ones use node-cron. Everything is saved to reminders.json.

const REMINDERS_PATH = path.resolve(__dirname, "../reminders.json");
const REMINDER_TICK_MS = 15_000;
const REMINDER_GRACE_MS = 24 * 60 * 60 * 1000; // fire missed reminders up to 24h late
const MAX_REMINDERS_PER_CHAT = 50;

interface Reminder {
  id: string;
  chatId: string | number;
  message: string;
  runAt?: number; // one-time
  cron?: string; // recurring
  runAgent: boolean; // true => run the agent with `message` as the instruction
  createdAt: number;
  firedAt?: number; // one-time reminders are kept 24h after firing so Snooze works
}

interface ReminderSpec {
  message: string;
  remind_at?: string;
  cron?: string;
  run_agent?: boolean;
}

const reminders = new Map<string, Reminder>();
const cronJobs = new Map<string, { stop: () => void }>();

function loadReminders(): void {
  try {
    const raw = JSON.parse(fs.readFileSync(REMINDERS_PATH, "utf-8")) as Reminder[];
    for (const r of raw) if (r && r.id && r.message) reminders.set(r.id, r);
  } catch {
    // no file yet
  }
}

function saveReminders(): void {
  try {
    fs.writeFileSync(REMINDERS_PATH, JSON.stringify([...reminders.values()], null, 2));
  } catch (err: any) {
    console.error("[REMINDERS] Could not save reminders.json:", err.message);
  }
}

function formatWhen(ms: number): string {
  return new Date(ms).toLocaleString("en-IN", {
    timeZone: TIMEZONE,
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  });
}

function describeReminder(r: Reminder): string {
  const when = r.cron ? `🔁 ${r.cron}` : `🕒 ${formatWhen(r.runAt as number)}`;
  return `${r.runAgent ? "🤖" : "⏰"} ${r.message}\n   ${when}`;
}

function scheduleCronReminder(r: Reminder): void {
  if (!r.cron || cronJobs.has(r.id)) return;
  if (!cron.validate(r.cron)) {
    console.error(`[REMINDERS] Invalid cron for ${r.id}: "${r.cron}"`);
    return;
  }
  const job = cron.schedule(
    r.cron,
    () => {
      fireReminder(r.id).catch((err) => console.error("[REMINDER FIRE ERROR]", err?.message || err));
    },
    { timezone: TIMEZONE }
  );
  cronJobs.set(r.id, job);
}

function removeReminder(id: string): void {
  cronJobs.get(id)?.stop();
  cronJobs.delete(id);
  reminders.delete(id);
  saveReminders();
}

// Returns a short note that is appended to the agent's reply
function registerReminder(chatId: string | number, spec: ReminderSpec): string {
  const message = String(spec.message || "").trim().slice(0, 500);
  if (!message) return "⚠️ Reminder skipped: empty message.";

  const active = [...reminders.values()].filter((r) => String(r.chatId) === String(chatId) && !r.firedAt);
  if (active.length >= MAX_REMINDERS_PER_CHAT) {
    return `⚠️ You already have ${MAX_REMINDERS_PER_CHAT} active reminders. Delete some with /reminders first.`;
  }

  const base = {
    id: crypto.randomUUID().slice(0, 8),
    chatId,
    message,
    runAgent: Boolean(spec.run_agent),
    createdAt: Date.now(),
  };

  let r: Reminder;
  if (spec.cron) {
    if (!cron.validate(spec.cron)) return `⚠️ I couldn't schedule that: "${spec.cron}" isn't a valid repeat pattern.`;
    r = { ...base, cron: spec.cron };
    scheduleCronReminder(r);
  } else if (spec.remind_at) {
    const runAt = Date.parse(spec.remind_at);
    if (Number.isNaN(runAt)) return "⚠️ I couldn't understand that reminder time.";
    if (runAt <= Date.now()) return "⚠️ That time has already passed, so I didn't set the reminder.";
    r = { ...base, runAt };
  } else {
    return "⚠️ Reminder skipped: no time given.";
  }

  reminders.set(r.id, r);
  saveReminders();
  return `✅ *${r.runAgent ? "Task scheduled" : "Reminder set"}*\n${describeReminder(r)}`;
}

function reminderButtons(r: Reminder) {
  if (r.cron) {
    return { inline_keyboard: [[{ text: "🗑 Stop repeating", callback_data: `rem:del:${r.id}` }]] };
  }
  return {
    inline_keyboard: [
      [
        { text: "😴 10 min", callback_data: `rem:s10:${r.id}` },
        { text: "😴 1 hour", callback_data: `rem:s60:${r.id}` },
        { text: "✅ Done", callback_data: `rem:done:${r.id}` },
      ],
    ],
  };
}

async function fireReminder(id: string, late = false): Promise<void> {
  const r = reminders.get(id);
  if (!r) return;

  if (r.runAgent) {
    const tk = getUserTokens(String(r.chatId)) || {};
    if (tk.access_token || tk.refresh_token) {
      await processUserPrompt({
        chatId: r.chatId,
        promptText: `[AUTOMATED:scheduled_task] ${r.message}`,
        tokens: tk,
        header: `🤖 *Scheduled task*${late ? " (late)" : ""}\n\n`,
      });
      return;
    }
    // no Google tokens: fall through and just notify
  }

  await sendTelegramMessage(
    r.chatId,
    `⏰ *Reminder*${late ? " (missed while I was offline)" : ""}\n\n${r.message}`,
    reminderButtons(r)
  );
}

async function reminderTick(): Promise<void> {
  const now = Date.now();
  let changed = false;

  for (const r of [...reminders.values()]) {
    if (r.cron) continue;

    if (r.firedAt) {
      if (now - r.firedAt > REMINDER_GRACE_MS) {
        reminders.delete(r.id);
        changed = true;
      }
      continue;
    }

    if (r.runAt !== undefined && r.runAt <= now) {
      r.firedAt = now; // mark first so it can never fire twice
      changed = true;
      if (now - r.runAt > REMINDER_GRACE_MS) continue; // too old, drop silently
      const late = now - r.runAt > 2 * 60 * 1000;
      fireReminder(r.id, late).catch((err) => console.error("[REMINDER FIRE ERROR]", err?.message || err));
    }
  }
  if (changed) saveReminders();
}

function initReminders(): void {
  loadReminders();
  for (const r of reminders.values()) scheduleCronReminder(r);
  setInterval(() => {
    reminderTick().catch((err) => console.error("[REMINDER TICK ERROR]", err?.message || err));
  }, REMINDER_TICK_MS);
  reminderTick().catch(() => {});
  console.log(`[REMINDERS] Loaded ${reminders.size} reminder(s).`);
}

async function sendRemindersList(chatId: string | number): Promise<void> {
  const mine = [...reminders.values()]
    .filter((r) => String(r.chatId) === String(chatId) && !r.firedAt)
    .sort((a, b) => (a.runAt ?? Infinity) - (b.runAt ?? Infinity));

  if (mine.length === 0) {
    await sendTelegramMessage(
      chatId,
      "⏰ You have no active reminders.\n\nJust tell me, e.g. _\"remind me at 6 PM to call Rahul\"_ or _\"every weekday at 9 AM send me my unread mails\"_."
    );
    return;
  }

  const text = "⏰ *Your reminders*\n\n" + mine.map((r, i) => `${i + 1}. ${describeReminder(r)}`).join("\n\n");
  const rows = mine.map((r, i) => [
    { text: `🗑 ${i + 1}. ${r.message.slice(0, 28)}`, callback_data: `rem:del:${r.id}` },
  ]);
  await sendTelegramMessage(chatId, text, { inline_keyboard: rows });
}

// ---------------------------------------------------------------------------
// Google Auth Prompt Helper
// ---------------------------------------------------------------------------

async function sendAuthPrompt(chatId: string | number): Promise<void> {
  const authUrl = getAuthUrl(String(chatId));
  try {
    await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
      chat_id: chatId,
      text: "🔐 *Google Authentication Required*\n\nOtis requires access to your Google Workspace (Gmail, Calendar, Drive, Sheets) to assist you.\n\nPlease link your Google account using the button below:",
      parse_mode: "Markdown",
      reply_markup: {
        inline_keyboard: [[{ text: "🔗 Connect Google Account", url: authUrl }]],
      },
    });
  } catch (err: any) {
    console.error("Failed to send auth prompt to Telegram:", err.response?.data || err.message);
  }
}

// ---------------------------------------------------------------------------
// Telegram Message & UI Helpers
// ---------------------------------------------------------------------------

function sanitizeMarkdown(text: string): string {
  if (!text) return "";
  // bullets
  let cleaned = text.replace(/^(\s*)-\s+/gm, "$1• ");
  // Telegram legacy Markdown uses single * for bold, so convert **bold** -> *bold*
  cleaned = cleaned.replace(/\*\*/g, "*");

  const starCount = (cleaned.match(/\*/g) || []).length;
  if (starCount % 2 !== 0) cleaned += "*";

  return cleaned;
}

async function sendTelegramMessage(
  chatId: string | number,
  text: string,
  replyMarkup?: any
): Promise<number | null> {
  try {
    const res = await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
      chat_id: chatId,
      text: sanitizeMarkdown(text),
      parse_mode: "Markdown",
      reply_markup: replyMarkup,
    });
    return res.data?.result?.message_id ?? null;
  } catch {
    try {
      const fallback = await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
        chat_id: chatId,
        text,
        reply_markup: replyMarkup,
      });
      return fallback.data?.result?.message_id ?? null;
    } catch {
      return null;
    }
  }
}

async function sendTelegramLocalPhoto(chatId: string | number, filePath: string, caption?: string): Promise<void> {
  try {
    const cleanPath = filePath.trim().replace(/^['"]|['"]$/g, "");
    if (!fs.existsSync(cleanPath)) {
      console.error(`[MEDIA ERROR] Photo file not found at: ${cleanPath}`);
      return;
    }

    const form = new FormData();
    form.append("chat_id", String(chatId));
    form.append("photo", fs.createReadStream(cleanPath), {
      filename: path.basename(cleanPath) || "image.jpg",
      contentType: cleanPath.endsWith(".png") ? "image/png" : "image/jpeg",
    });

    if (caption) {
      form.append("caption", sanitizeMarkdown(caption));
      form.append("parse_mode", "Markdown");
    }

    await axios.post(`${TELEGRAM_API_BASE}/sendPhoto`, form, {
      headers: form.getHeaders(),
    });
    console.log(`[MEDIA] Successfully sent local photo: ${cleanPath}`);
  } catch (err: any) {
    console.error("Failed to send local photo:", err.response?.data || err.message);
  }
}

async function editTelegramMessage(
  chatId: string | number,
  messageId: number,
  text: string,
  replyMarkup?: any
): Promise<void> {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/editMessageText`, {
      chat_id: chatId,
      message_id: messageId,
      text: sanitizeMarkdown(text),
      parse_mode: "Markdown",
      reply_markup: replyMarkup,
    });
  } catch {
    try {
      await axios.post(`${TELEGRAM_API_BASE}/editMessageText`, {
        chat_id: chatId,
        message_id: messageId,
        text,
        reply_markup: replyMarkup,
      });
    } catch {
      // Ignored: expired edit or unchanged content
    }
  }
}

// Swaps only the buttons under a message and leaves its text untouched
async function editTelegramReplyMarkup(chatId: string | number, messageId: number, replyMarkup: any): Promise<void> {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/editMessageReplyMarkup`, {
      chat_id: chatId,
      message_id: messageId,
      reply_markup: replyMarkup,
    });
  } catch {
    // Ignored: message too old or buttons unchanged
  }
}

async function sendTelegramPhoto(chatId: string | number, photoUrl: string, caption?: string): Promise<void> {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/sendPhoto`, {
      chat_id: chatId,
      photo: photoUrl,
      caption: caption ? sanitizeMarkdown(caption) : undefined,
      parse_mode: "Markdown",
    });
  } catch (err: any) {
    console.error("Failed to send remote photo:", err.response?.data || err.message);
  }
}

async function sendTelegramBase64Photo(chatId: string | number, base64DataUri: string, caption?: string): Promise<void> {
  try {
    const base64Clean = base64DataUri.replace(/^data:image\/\w+;base64,/, "");
    const imageBuffer = Buffer.from(base64Clean, "base64");

    const form = new FormData();
    form.append("chat_id", String(chatId));
    form.append("photo", imageBuffer, {
      filename: "image.png",
      contentType: "image/png",
    });
    if (caption) {
      form.append("caption", sanitizeMarkdown(caption));
      form.append("parse_mode", "Markdown");
    }

    await axios.post(`${TELEGRAM_API_BASE}/sendPhoto`, form, {
      headers: form.getHeaders(),
    });
  } catch (err: any) {
    console.error("Failed to send base64 photo:", err.response?.data || err.message);
  }
}

async function sendTelegramApprovalMessage(chatId: string | number, text: string, actionId: string): Promise<void> {
  const keyboard = {
    inline_keyboard: [
      [
        { text: "✅ Send Email", callback_data: `confirm:${actionId}` },
        { text: "✏️ Revise", callback_data: `revise:${actionId}` },
        { text: "❌ Cancel", callback_data: `cancel:${actionId}` },
      ],
    ],
  };

  try {
    await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
      chat_id: chatId,
      text: sanitizeMarkdown(text),
      parse_mode: "Markdown",
      reply_markup: keyboard,
    });
  } catch {
    try {
      await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
        chat_id: chatId,
        text,
        reply_markup: keyboard,
      });
    } catch (err: any) {
      console.error("Failed to send approval message:", err.response?.data || err.message);
    }
  }
}

async function answerCallbackQuery(callbackQueryId: string, text?: string, showAlert: boolean = false): Promise<void> {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/answerCallbackQuery`, {
      callback_query_id: callbackQueryId,
      text,
      show_alert: showAlert,
    });
  } catch (err: any) {
    console.error("Failed to answer callback query:", err.response?.data || err.message);
  }
}

// ---------------------------------------------------------------------------
// Agent Dispatch Pipeline with SSE & Coordinates
// ---------------------------------------------------------------------------

interface PromptParams {
  chatId: string | number;
  promptText: string;
  tokens: any;
  audioPayload?: any;
  imagePayload?: any;
  locationPayload?: { latitude: number; longitude: number } | null;
  // silent = scheduled/proactive run: no "thinking" message, no error message,
  // and nothing is sent if the agent replies [NO_ALERT]
  silent?: boolean;
  // text put in front of the agent's reply (e.g. "☀️ Morning Briefing")
  header?: string;
}

// Public entry point: queues the run so only one agent call per chat is active at a time
function processUserPrompt(params: PromptParams): Promise<void> {
  return runExclusive(params.chatId, () => runAgent(params));
}

async function runAgent(params: PromptParams): Promise<void> {
  const { chatId, promptText, tokens, audioPayload, imagePayload, locationPayload, silent = false, header } = params;

  let loaderMessageId: number | null = silent ? null : await sendTelegramMessage(chatId, "⚡ Otis is thinking...");

  try {
    const streamResponse = await axios.post(
      `${PYTHON_AGENT_URL}-stream`,
      {
        prompt: promptText,
        chat_id: String(chatId),
        google_tokens: tokens,
        audio: audioPayload,
        image: imagePayload,
        location: locationPayload || undefined,
      },
      { responseType: "stream" }
    );

    let finalReply = "";
    let lastStatusText = "⚡ Otis is thinking...";
    let lastEditTimestamp = 0;
    const stagedApprovalHolder: { data: { action: any; text: string } | null } = { data: null };
    const pollHolder: { data: PollSpec | null } = { data: null };
    const reminderSpecs: ReminderSpec[] = [];
    const RATE_LIMIT_MS = 1200;

    const updateStatusSafe = async (newText: string) => {
      const now = Date.now();
      if (newText !== lastStatusText && now - lastEditTimestamp >= RATE_LIMIT_MS) {
        lastStatusText = newText;
        lastEditTimestamp = now;
        if (loaderMessageId) {
          await editTelegramMessage(chatId, loaderMessageId, newText);
        }
      }
    };

    const parser = createParser({
      onEvent: (event: EventSourceMessage) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.type === "status" && payload.message) {
            updateStatusSafe(payload.message);
          } else if (payload.type === "approval" && payload.action) {
            stagedApprovalHolder.data = { action: payload.action, text: payload.text || "" };
          } else if (payload.type === "poll" && payload.poll) {
            pollHolder.data = payload.poll;
            } else if (payload.type === "reminder" && payload.reminder) {
            reminderSpecs.push(payload.reminder);
          } else if (payload.type === "final" && payload.response) {
            finalReply = payload.response;
          }
        } catch {
          // Ignore incomplete chunks
        }
      },
    });

    streamResponse.data.on("data", (chunk: Buffer) => {
      parser.feed(chunk.toString("utf-8"));
    });

    await new Promise((resolve, reject) => {
      streamResponse.data.on("end", resolve);
      streamResponse.data.on("error", reject);
    });

        const reminderNotes = reminderSpecs.map((s) => registerReminder(chatId, s));
    const replyIsEmpty = !finalReply || !finalReply.trim();
    const replyIsNoAlert = !replyIsEmpty && finalReply.trim().startsWith(NO_ALERT_TOKEN);

    // Scheduled run with nothing worth reporting: stay quiet
    if (silent && (replyIsEmpty || replyIsNoAlert)) {
      console.log("[PROACTIVE] Nothing new to report.");
      return;
    }

    if (replyIsNoAlert) {
      finalReply = "✅ All clear. Nothing needs your attention right now.";
    } else if (replyIsEmpty) {
      finalReply = "✅ Action completed.";
    }

    if (header) {
      finalReply = header + finalReply;
    }

        if (reminderNotes.length) {
      finalReply += "\n\n" + reminderNotes.join("\n");
    }

    // 1. Process Dedicated Approval Events or Regex Fallback
    const approvalMatch = finalReply.match(/\[APPROVAL_REQUIRED:([\s\S]*?)\]/);
    const staged = stagedApprovalHolder.data;

    if (staged || approvalMatch) {
      try {
        const actionData = staged ? staged.action : JSON.parse(approvalMatch![1]);
        const promptTextClean = staged
          ? staged.text || finalReply
          : finalReply.replace(/\[APPROVAL_REQUIRED:[\s\S]*?\]/, "").trim();

        const actionId = crypto.randomUUID().slice(0, 8);
        pendingActions.set(actionId, { chatId, actionData, tokens });

        if (loaderMessageId) {
          await editTelegramMessage(chatId, loaderMessageId, "⏳ Awaiting confirmation...");
        }
        await sendTelegramApprovalMessage(chatId, promptTextClean, actionId);
        return;
      } catch (err) {
        console.error("Failed to parse approval data:", err);
      }
    }

    // 2. Poll handling (SSE "poll" event or [POLL: {...}] tag fallback)
    const { poll: tagPoll, cleaned: replyWithoutPoll } = extractPollFromReply(finalReply);
    const pollToSend = pollHolder.data || tagPoll;

    if (pollToSend) {
      const intro = replyWithoutPoll.trim() || "📊 Quick question:";
      if (loaderMessageId) {
        await editTelegramMessage(chatId, loaderMessageId, intro);
      } else {
        await sendTelegramMessage(chatId, intro);
      }
      await sendTelegramPoll(chatId, pollToSend);
      return;
    }

    // 3. Resilient Visual Media Extractor (handles local paths & URLs regardless of Gemini tags)
    const localFileMatch = finalReply.match(
      /(?:\[IMAGE_(?:PATH|URL):\s*([a-zA-Z]:\\[^\]\r\n]+|\/[^\]\r\n]+)\]|(?:IMAGE_(?:PATH|URL):\s*)?([a-zA-Z]:\\[^\s\r\n]+\.(?:jpg|jpeg|png|webp)|\/[^\s\r\n]+\.(?:jpg|jpeg|png|webp)))/i
    );
    const remoteUrlMatch = finalReply.match(/\[IMAGE_URL:\s*(https?:\/\/[^\s\]]+)\]/i);
    const base64Match = finalReply.match(/\[IMAGE_BASE64:\s*([^\]]+)\]/i);

    if (localFileMatch) {
      const filePath = (localFileMatch[1] || localFileMatch[2]).trim().replace(/^['"]|['"]$/g, "");
      const cleanReply = finalReply
        .replace(localFileMatch[0], "")
        .replace(/\[IMAGE_(?:PATH|URL):[^\]]+\]/gi, "")
        .trim();

      if (loaderMessageId) {
        await editTelegramMessage(chatId, loaderMessageId, "🎨 Image ready:");
      }
      await sendTelegramLocalPhoto(chatId, filePath, cleanReply || "Here is your generated image.");
    } else if (remoteUrlMatch) {
      const photoUrl = remoteUrlMatch[1].trim();
      const cleanReply = finalReply.replace(/\[IMAGE_URL:\s*https?:\/\/[^\s\]]+\]/gi, "").trim();

      if (loaderMessageId) {
        await editTelegramMessage(chatId, loaderMessageId, "📸 Image retrieved:");
      }
      await sendTelegramPhoto(chatId, photoUrl, cleanReply);
    } else if (base64Match) {
      const dataUri = base64Match[1].trim();
      const cleanReply = finalReply.replace(/\[IMAGE_BASE64:\s*[^\]]+\]/gi, "").trim();

      if (loaderMessageId) {
        await editTelegramMessage(chatId, loaderMessageId, "🎨 Generated visual:");
      }
      await sendTelegramBase64Photo(chatId, dataUri, cleanReply);
    } else {
      // Normal text reply, with the compact tool menu attached underneath
      if (loaderMessageId) {
        await editTelegramMessage(chatId, loaderMessageId, finalReply, buildCompactMenu());
      } else {
        await sendTelegramMessage(chatId, finalReply, buildCompactMenu());
      }
    }
  } catch (err: any) {
    console.error("Agent Dispatch Error:", err.message);
    if (silent) return; // never bother you with errors from background checks

    const errMsg = "⚠️ Otis encountered an issue executing this request.";
    if (loaderMessageId) {
      await editTelegramMessage(chatId, loaderMessageId, errMsg);
    } else {
      await sendTelegramMessage(chatId, errMsg);
    }
  }
}

// ---------------------------------------------------------------------------
// Proactive mode: the bot messages you first
// ---------------------------------------------------------------------------
//
// 1. Morning briefing   (MORNING_CRON)   + a "where do you start?" poll
// 2. Heartbeat          (HEARTBEAT_CRON) only speaks if something NEW needs you
// 3. Evening wrap-up    (WRAPUP_CRON)    + a poll about unfinished tasks
//
// All of them use the same conversation as your chat, so the agent remembers what it
// already told you and doesn't repeat alerts. Poll answers flow back through
// handlePollVote -> agent, which then acts on your choice.

type ProactiveKind = "morning" | "wrapup" | "heartbeat";

const PROACTIVE: Record<ProactiveKind, { header: string; prompt: string }> = {
  morning: {
    header: "☀️ *Morning Briefing*\n\n",
    prompt:
      "[AUTOMATED:morning_briefing] Run the morning executive briefing now. Fetch today's calendar, unread priority emails and pending tasks in parallel. " +
      "If there are 2 to 4 clear priorities for today, end with ONE poll asking which one I should start with (options = those priorities).",
  },
  wrapup: {
    header: "🌙 *Evening Wrap-up*\n\n",
    prompt:
      "[AUTOMATED:evening_wrapup] Run the evening wrap-up now. Fetch in parallel: tasks still pending today, tomorrow's calendar, and unread important emails. " +
      "Give a short summary: what's still pending, tomorrow's first events, and anything urgent in email. " +
      "If tasks are still pending, end with ONE poll asking what to do (for example which to do first tomorrow). If nothing is pending, no poll.",
  },
  heartbeat: {
    header: "🔔 *Heads up*\n\n",
    prompt:
      "[AUTOMATED:heartbeat] Silent background check. In parallel: (1) calendar events starting within the next 45 minutes, " +
      "(2) unread emails that look important or need a reply, (3) tasks that are overdue or due today. " +
      "Only report items that are NEW: do not repeat anything you already alerted me about earlier in this conversation. " +
      "If there is nothing new worth interrupting me for, reply with exactly [NO_ALERT] and nothing else. " +
      "Otherwise send a SHORT alert (max 6 lines). If I need to choose how to handle something (reply now / remind later / ignore), end with ONE poll.",
  },
};

async function runProactive(kind: ProactiveKind, chatIdOverride?: string | number, silent = true): Promise<void> {
  const chatId = chatIdOverride ?? process.env.TELEGRAM_ALLOWED_USER_ID;
  if (!chatId) {
    console.log(`[PROACTIVE:${kind} SKIPPED] TELEGRAM_ALLOWED_USER_ID is missing in .env.`);
    return;
  }

  const userTokens = getUserTokens(String(chatId));
  const hasUserTokens = Boolean(userTokens && (userTokens.access_token || userTokens.refresh_token));
  if (!hasUserTokens) {
    console.log(`[PROACTIVE:${kind} SKIPPED] No authenticated tokens for chat ${chatId}.`);
    return;
  }

  console.log(`[PROACTIVE:${kind}] Running...`);
  await processUserPrompt({
    chatId,
    promptText: PROACTIVE[kind].prompt,
    tokens: userTokens,
    silent,
    header: PROACTIVE[kind].header,
  });
}

function scheduleProactive(expr: string, kind: ProactiveKind): void {
  if (!PROACTIVE_ENABLED) return;
  if (!cron.validate(expr)) {
    console.error(`[PROACTIVE] Invalid cron expression for ${kind}: "${expr}"`);
    return;
  }
  cron.schedule(
    expr,
    () => {
      runProactive(kind).catch((err) => console.error(`[PROACTIVE:${kind} ERROR]`, err?.message || err));
    },
    { timezone: TIMEZONE }
  );
  console.log(`[PROACTIVE] ${kind} scheduled: "${expr}" (${TIMEZONE})`);
}

scheduleProactive(MORNING_CRON, "morning");
scheduleProactive(HEARTBEAT_CRON, "heartbeat");
scheduleProactive(WRAPUP_CRON, "wrapup");

// ---------------------------------------------------------------------------
// OAuth Callback Route
// ---------------------------------------------------------------------------

app.get("/auth/google/callback", async (req: Request, res: Response) => {
  const code = req.query.code as string;
  const chatId = req.query.state as string;

  if (!code || !chatId) {
    return res.status(400).send("<h3>Missing authorization code or chat state.</h3>");
  }

  try {
    const { tokens: tokenData } = await oauth2Client.getToken(code);

    const existing = getUserTokens(String(chatId));
    if (!tokenData.refresh_token && existing?.refresh_token) {
      tokenData.refresh_token = existing.refresh_token;
    }

    saveUserTokens(String(chatId), tokenData);
    console.log(`[AUTH] Successfully saved credentials for chat ${chatId}.`);

    await sendTelegramMessage(
      chatId,
      "✅ *Google Workspace Connected Successfully!*\n\nOtis now has access to your Gmail, Calendar, Drive, and Sheets. What would you like to do?",
      buildMainMenu()
    );

    return res.send(`
      <div style="font-family: Arial, sans-serif; text-align: center; padding-top: 50px;">
        <h2>Authentication Successful!</h2>
        <p>Your Google account has been connected to Otis. You may close this window and return to Telegram.</p>
      </div>
    `);
  } catch (err: any) {
    console.error("[AUTH CALLBACK ERROR]:", err.response?.data || err.message);
    return res.status(500).send("<h3>Authentication failed. Please verify console logs and try again.</h3>");
  }
});

// ---------------------------------------------------------------------------
// Telegram Webhook Handler
// ---------------------------------------------------------------------------

app.post("/webhook/telegram", async (req: Request, res: Response) => {
  res.sendStatus(200);

  const body = req.body;

  // -------------------------------------------------------------------------
  // 1. Inline Button Clicks
  // -------------------------------------------------------------------------
  if (body.callback_query) {
    const cq = body.callback_query;
    const data: string = cq.data || "";
    const messageId = cq.message?.message_id;
    const chatId = cq.message?.chat?.id;

    console.log(`\n🔘 [BUTTON CLICK DETECTED] callback_data: "${data}" from chat: ${chatId}`);

    // 1a. Menu navigation (menu:main | menu:compact | menu:<category>)
    if (data.startsWith("menu:")) {
      const target = data.slice(5);
      let markup: any = null;
      if (target === "main") markup = buildMainMenu();
      else if (target === "compact") markup = buildCompactMenu();
      else if (CATEGORIES[target]) markup = buildCategoryMenu(target);

      await answerCallbackQuery(cq.id);
      if (markup && chatId && messageId) {
        await editTelegramReplyMarkup(chatId, messageId, markup);
      }
      return;
    }

        // 1a-2. Reminder buttons (rem:list | rem:s10:<id> | rem:s60:<id> | rem:done:<id> | rem:del:<id>)
    if (data.startsWith("rem:")) {
      const [, action, remId] = data.split(":");
      await answerCallbackQuery(cq.id);
      if (!chatId) return;

      if (action === "list") {
        await sendRemindersList(chatId);
        return;
      }

      const r = reminders.get(remId);
      if (!r || String(r.chatId) !== String(chatId)) {
        if (messageId) await editTelegramMessage(chatId, messageId, "⚠️ That reminder no longer exists.");
        return;
      }

      if (action === "s10" || action === "s60") {
        const mins = action === "s10" ? 10 : 60;
        const runAt = Date.now() + mins * 60 * 1000;
        const snoozed: Reminder = {
          id: crypto.randomUUID().slice(0, 8),
          chatId: r.chatId,
          message: r.message,
          runAt,
          runAgent: r.runAgent,
          createdAt: Date.now(),
        };
        reminders.set(snoozed.id, snoozed);
        saveReminders();
        if (messageId) {
          await editTelegramMessage(chatId, messageId, `😴 Snoozed. I'll remind you again at ${formatWhen(runAt)}.\n\n${r.message}`);
        }
      } else if (action === "done") {
        if (messageId) await editTelegramMessage(chatId, messageId, `✅ Done: ${r.message}`);
      } else if (action === "del") {
        removeReminder(remId);
        if (messageId) await editTelegramMessage(chatId, messageId, `🗑 Deleted: ${r.message}`);
      }
      return;
    }

    // 1b. Tool buttons (qa:<key>)
    if (data.startsWith("qa:")) {
      const key = data.slice(3);
      const item = ITEMS[key];

      if (!item || !chatId) {
        await answerCallbackQuery(cq.id, "Unknown action.");
        return;
      }

      // Tapping any tool cancels a previous half-finished input request
      pendingInputs.delete(String(chatId));

      const btnTokens = getUserTokens(String(chatId)) || {};
      const btnHasTokens = Boolean(btnTokens && (btnTokens.access_token || btnTokens.refresh_token));
      if (!btnHasTokens) {
        await answerCallbackQuery(cq.id, "Please connect your Google account first.", true);
        await sendAuthPrompt(chatId);
        return;
      }

      // Tool needs details from you: ask, then wait for your next text message
      if (item.ask) {
        await answerCallbackQuery(cq.id);
        pendingInputs.set(String(chatId), { key, expires: Date.now() + INPUT_TTL_MS });
        await sendTelegramMessage(chatId, `${item.label}\n\n✍️ ${item.ask}\n\nSend /cancel to abort.`, {
          force_reply: true,
          input_field_placeholder: "Type here...",
        });
        return;
      }

      // One-tap tool: run immediately
      await answerCallbackQuery(cq.id, `Fetching: ${item.label}`);
      await processUserPrompt({
        chatId,
        promptText: item.prompt,
        tokens: btnTokens,
      });
      return;
    }

    // 1c. Email approval buttons (confirm / revise / cancel)
    const [actionType, actionId] = data.split(":");
    const pending = pendingActions.get(actionId);

    if (!pending) {
      console.warn(`⚠️ [BUTTON REJECTED] actionId "${actionId}" not found in pendingActions. (Map size: ${pendingActions.size})`);
      await answerCallbackQuery(cq.id, "⚠️ This draft expired. Please ask Otis to draft it again.", true);
      if (messageId && chatId) {
        await editTelegramMessage(chatId, messageId, "⚠️ This draft request expired. Please re-trigger the action.");
      }
      return;
    }

    if (actionType === "cancel") {
      console.log(`❌ [ACTION CANCELLED] actionId: ${actionId}`);
      pendingActions.delete(actionId);
      await answerCallbackQuery(cq.id, "Action cancelled.");
      if (messageId && chatId) {
        await editTelegramMessage(chatId, messageId, "❌ Staged email draft was cancelled.", buildCompactMenu());
      }
      return;
    }

    if (actionType === "revise") {
      pendingActions.delete(actionId);
      await answerCallbackQuery(cq.id, "Draft kept for revision.");
      if (messageId && chatId) {
        await editTelegramMessage(
          chatId,
          messageId,
          "✏️ *Draft held for edits.*\n\nSend your instructions (e.g. _\"Make it more casual\"_, _\"Change subject to Follow-up\"_, or _\"Attach an image of an invoice\"_), and Otis will update it."
        );
      }
      return;
    }

    if (actionType === "confirm") {
      console.log(`🚀 [SENDING TO PYTHON] Calling ${PYTHON_CONFIRM_URL}...`);
      await answerCallbackQuery(cq.id, "Sending email...");
      if (messageId && chatId) {
        await editTelegramMessage(chatId, messageId, "🚀 Sending email...");
      }

      try {
        const activeTokens = getUserTokens(String(chatId)) || pending.tokens;

        const response = await axios.post(PYTHON_CONFIRM_URL, {
          chat_id: chatId,
          google_tokens: activeTokens,
          action_data: pending.actionData,
        });

        console.log(`✅ [PYTHON SUCCESS RESPONSE]:`, response.data);
        pendingActions.delete(actionId);

        const replyResult = response.data?.result || "✅ Email dispatched.";
        if (messageId && chatId) {
          await editTelegramMessage(chatId, messageId, replyResult, buildCompactMenu());
        }
      } catch (err: any) {
        const errorDetail = err.response?.data?.detail || err.response?.data?.error || err.message;
        console.error(`❌ [PYTHON POST FAILED]:`, errorDetail);
        if (messageId && chatId) {
          await editTelegramMessage(chatId, messageId, `❌ Failed to dispatch email: ${errorDetail}`);
        }
      }
    }
    return;
  }

  // -------------------------------------------------------------------------
  // 2. Poll Answers (you voted in a poll the bot sent)
  // -------------------------------------------------------------------------
  if (body.poll_answer) {
    const pa = body.poll_answer;
    const pollId: string = pa.poll_id;
    const optionIds: number[] = Array.isArray(pa.option_ids) ? pa.option_ids : [];

    console.log(`🗳️ [POLL ANSWER] poll ${pollId} options ${JSON.stringify(optionIds)}`);

    if (!pendingPolls.has(pollId)) {
      // Poll is too old or not tracked. In a private chat user.id is the chat id.
      const fallbackChat = pa.user?.id;
      if (fallbackChat) {
        await sendTelegramMessage(
          fallbackChat,
          "⚠️ I lost track of that poll (it may be too old). Please tell me your choice in a message."
        );
      }
      return;
    }

    // Quiz answers are final (Telegram locks them), so grade immediately
    if (pendingPolls.get(pollId)?.correctIndex !== undefined) {
      handlePollVote(pollId, optionIds).catch((err) =>
        console.error("[QUIZ VOTE ERROR]:", err?.message || err)
      );
      return;
    }

    // Wait until the user stops tapping, then act on the final choice
    const existingTimer = pollTimers.get(pollId);
    if (existingTimer) clearTimeout(existingTimer);

    pollTimers.set(
      pollId,
      setTimeout(() => {
        pollTimers.delete(pollId);
        handlePollVote(pollId, optionIds).catch((err) =>
          console.error("[POLL VOTE ERROR]:", err?.message || err)
        );
      }, POLL_DEBOUNCE_MS)
    );
    return;
  }

  // -------------------------------------------------------------------------
  // 3. Incoming Messages
  // -------------------------------------------------------------------------
  if (!body.message) return;

  const msg = body.message;
  const chatId = msg.chat.id;

  const tokens = getUserTokens(String(chatId)) || {};
  const hasTokens = Boolean(tokens && (tokens.access_token || tokens.refresh_token));

  if (msg.text === "/auth" || msg.text === "/login") {
    await sendAuthPrompt(chatId);
    return;
  }

  if (msg.text === "/start") {
    if (!hasTokens) {
      await sendAuthPrompt(chatId);
    } else {
      await sendQuickMenu(chatId, "👋 Otis online. Pick a tool below or just tell me what you need.");
    }
    return;
  }

  if (!hasTokens) {
    await sendAuthPrompt(chatId);
    return;
  }

  // /menu -> show all tools
  if (typeof msg.text === "string" && /^\/menu(?:@\w+)?$/i.test(msg.text.trim())) {
    pendingInputs.delete(String(chatId));
    await sendQuickMenu(chatId);
    return;
  }

  // /briefing, /wrapup, /check -> run the proactive routines on demand (handy for testing)
  if (typeof msg.text === "string") {
    const cmd = msg.text.trim().toLowerCase().replace(/@\w+$/, "");
    if (cmd === "/briefing") {
      pendingInputs.delete(String(chatId));
      await runProactive("morning", chatId, false);
      return;
    }
    if (cmd === "/wrapup") {
      pendingInputs.delete(String(chatId));
      await runProactive("wrapup", chatId, false);
      return;
    }

        if (cmd === "/reminders") {
      pendingInputs.delete(String(chatId));
      await sendRemindersList(chatId);
      return;
    }

    if (cmd === "/check") {
      pendingInputs.delete(String(chatId));
      await processUserPrompt({ chatId, promptText: ITEMS.check.prompt, tokens });
      return;
    }
  }

  // /cancel -> abort a tool that is waiting for details
  if (typeof msg.text === "string" && /^\/cancel(?:@\w+)?$/i.test(msg.text.trim())) {
    pendingInputs.delete(String(chatId));
    await sendTelegramMessage(chatId, "❌ Cancelled.", buildCompactMenu());
    return;
  }

  // A tool button is waiting for details: use this message as the input
  const waiting = pendingInputs.get(String(chatId));
  if (waiting) {
    pendingInputs.delete(String(chatId));
    const text = typeof msg.text === "string" ? msg.text.trim() : "";
    if (waiting.expires > Date.now() && text && !text.startsWith("/")) {
      const item = ITEMS[waiting.key];
      if (item) {
        await processUserPrompt({
          chatId,
          promptText: item.prompt.split("{input}").join(text),
          tokens,
        });
        return;
      }
    }
    // Otherwise (expired, command, voice/photo/location...) fall through and handle normally
  }

  let promptText = msg.text || msg.caption || "";
  let audioPayload: any = null;
  let imagePayload: any = null;
  let locationPayload: { latitude: number; longitude: number } | null = null;

  // Polls forwarded TO the bot
  if (msg.poll) {
    const p = msg.poll;
    const opts = (p.options || []).map((o: any, i: number) => `${i + 1}. ${o.text}`).join("\n");
    promptText =
      `I shared a poll with you.\nQuestion: ${p.question}\nOptions:\n${opts}\n\n` +
      (promptText ? `${promptText}\n\n` : "") +
      "Give me your recommendation on which option is best and why, in a short answer.";
  }

  // Real-time Coordinates & Location Pins
  if (msg.location) {
    locationPayload = {
      latitude: msg.location.latitude,
      longitude: msg.location.longitude,
    };
    if (!promptText) {
      promptText = `I have shared my current coordinates: Latitude ${msg.location.latitude}, Longitude ${msg.location.longitude}. Use these coordinates for any local queries, navigation, or weather requests.`;
    }
  }

  // Voice Notes & Audio
  if (msg.voice || msg.audio) {
    const fileId = msg.voice?.file_id || msg.audio?.file_id;
    try {
      const fileRes = await axios.get(`${TELEGRAM_API_BASE}/getFile?file_id=${fileId}`);
      const filePath = fileRes.data?.result?.file_path;
      const fileDownload = await axios.get(`https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${filePath}`, {
        responseType: "arraybuffer",
      });

      audioPayload = {
        data: Buffer.from(fileDownload.data).toString("base64"),
        mime_type: msg.voice ? "audio/ogg" : "audio/mpeg",
      };
      if (!promptText) promptText = "Listen to this audio note and execute any requests.";
    } catch (e: any) {
  console.error("Audio download error:", e.message);
  await sendTelegramMessage(chatId, "⚠️ I couldn't download that voice note. Please try again.");
  return;
}
  }

  // Images & Photos
  if (msg.photo && msg.photo.length > 0) {
    const bestPhoto = msg.photo[msg.photo.length - 1];
    try {
      const fileRes = await axios.get(`${TELEGRAM_API_BASE}/getFile?file_id=${bestPhoto.file_id}`);
      const filePath = fileRes.data?.result?.file_path;
      const fileDownload = await axios.get(`https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${filePath}`, {
        responseType: "arraybuffer",
      });

      imagePayload = {
        data: Buffer.from(fileDownload.data).toString("base64"),
        mime_type: "image/jpeg",
      };
      if (!promptText) promptText = "Analyze this image and execute any relevant tools.";
    }  catch (e: any) {
  console.error("Image error:", e.message);
  await sendTelegramMessage(chatId, "⚠️ I couldn't Process With Image for now. Please try again.");
  return;
}
  }

  if (!promptText && !audioPayload && !imagePayload && !locationPayload) return;

  await processUserPrompt({
    chatId,
    promptText,
    tokens,
    audioPayload,
    imagePayload,
    locationPayload,
  });
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

async function registerWebhook(): Promise<void> {
  if (!WEBHOOK_URL) {
    console.log(
      "[WEBHOOK] WEBHOOK_URL not set. Make sure your webhook was registered with " +
        'allowed_updates ["message","callback_query","poll_answer"], otherwise poll votes will not arrive.'
    );
    return;
  }
  try {
    const res = await axios.post(`${TELEGRAM_API_BASE}/setWebhook`, {
      url: `${WEBHOOK_URL.replace(/\/$/, "")}/webhook/telegram`,
      allowed_updates: ["message", "callback_query", "poll_answer"],
    });
    console.log("[WEBHOOK] setWebhook:", res.data?.description || "ok");
  } catch (err: any) {
    console.error("[WEBHOOK] Failed to register webhook:", err.response?.data || err.message);
  }
}

app.listen(PORT, () => {
    initReminders();
  console.log(`🚀 Otis Node.js Gateway running on port ${PORT}`);
  console.log(`[POLLS] Tracking ${pendingPolls.size} open poll(s) from polls.json`);
  registerWebhook();
});