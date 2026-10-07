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


const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || "http://localhost:8000";
const TOKENS_PATH = path.resolve(__dirname, "../tokens.json");

// Staged actions storage
const pendingActions = new Map<string, { chatId: string | number; actionData: any; tokens: any }>();

// ---------------------------------------------------------------------------
// Google Auth Prompt Helper
// ---------------------------------------------------------------------------




cron.schedule("0 8 * * 1-5", async () => {
  console.log("[CRON] Triggering Autonomous Morning Briefing...");
  
  try {
    const chatId = process.env.TELEGRAM_ALLOWED_USER_ID;
    if (!chatId) {
      console.log("[CRON SKIPPED] TELEGRAM_ALLOWED_USER_ID is missing in .env.");
      return;
    }

    // Retrieve decrypted tokens directly using your helper
    const userTokens = getUserTokens(chatId);
    if (!userTokens) {
      console.log(`[CRON SKIPPED] No authenticated tokens found for chat ID: ${chatId}.`);
      return;
    }

    const briefingPrompt = `CRITICAL TASK: Execute a complete workspace scan for today's morning briefing. 
You MUST call the following tools sequentially to gather live data:
1. Call your calendar tool to fetch today's scheduled events.
2. Call your Gmail tool to check unread priority emails.
3. Call your Google Tasks tool to check pending tasks due today.

Do not guess or skip any tool. Once you have the live tool outputs, synthesize them into a clean, structured Executive Briefing with emojis.`;

    const todayDateStr = new Date().toISOString().split("T")[0];
    const uniqueThreadId = `cron_briefing_${chatId}_${todayDateStr}`;

    const response = await axios.post(
      `${PYTHON_BACKEND_URL}/api/agent/dispatch-stream`,
      {
        prompt: briefingPrompt,
        google_tokens: userTokens, // Clean, decrypted token object
        chat_id: chatId,
        thread_id: uniqueThreadId,
      },
      {
        responseType: "text",
        timeout: 90000,
      }
    );

    // Parse SSE lines from dispatch-stream
    const rawOutput = response.data;
    let finalBriefingText = "";

    try {
      const lines = rawOutput.split("\n");
      for (const line of lines) {
        if (line.startsWith("data: ")) {
          const jsonStr = line.replace("data: ", "").trim();
          const parsed = JSON.parse(jsonStr);
          if (parsed.response) {
            finalBriefingText = parsed.response;
          }
        }
      }
    } catch {
      finalBriefingText = rawOutput;
    }

    if (!finalBriefingText) {
      finalBriefingText = "All systems operational, but no specific workspace updates were returned.";
    }

    const headerMsg = "☀️ * Morning  Briefing*\n\n";
    await sendTelegramMessage(chatId, headerMsg + finalBriefingText);
    console.log("[CRON] Detailed morning briefing successfully dispatched to Telegram.");

  } catch (err: any) {
    console.error("[CRON ERROR] Failed to generate morning briefing:", err.response?.data || err.message);
  }
});

async function sendAuthPrompt(chatId: string | number): Promise<void> {
  const authUrl = getAuthUrl(String(chatId));
  try {
    await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
      chat_id: chatId,
      text: "🔐 **Google Authentication Required**\n\nOtis requires access to your Google Workspace (Gmail, Calendar, Drive, Sheets) to assist you.\n\nPlease link your Google account using the button below:",
      parse_mode: "markdown-escape",
      reply_markup: {
        inline_keyboard: [
          [{ text: "🔗 Connect Google Account", url: authUrl }]
        ],
      },
    });
  } catch (err: any) {
    console.error("Failed to send auth prompt to Telegram:", err.message);
  }
}

// ---------------------------------------------------------------------------
// Telegram Message & UI Helpers
// ---------------------------------------------------------------------------

function sanitizeMarkdown(text: string): string {
  if (!text) return "";
  let cleaned = text.replace(/^(\s*)-\s+/gm, "$1• ");

  const boldCount = (cleaned.match(/\*\*/g) || []).length;
  if (boldCount % 2 !== 0) cleaned += "**";

  const italicCount = (cleaned.match(/(?<!\*)\*(?!\*)/g) || []).length;
  if (italicCount % 2 !== 0) cleaned += "*";

  return cleaned;
}

async function sendTelegramMessage(chatId: string | number, text: string): Promise<number | null> {
  try {
    const res = await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
      chat_id: chatId,
      text: sanitizeMarkdown(text),
      parse_mode: "Markdown",
    });
    return res.data?.result?.message_id ?? null;
  } catch {
    try {
      const fallback = await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
        chat_id: chatId,
        text,
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

async function editTelegramMessage(chatId: string | number, messageId: number, text: string): Promise<void> {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/editMessageText`, {
      chat_id: chatId,
      message_id: messageId,
      text: sanitizeMarkdown(text),
      parse_mode: "Markdown",
    });
  } catch {
    try {
      await axios.post(`${TELEGRAM_API_BASE}/editMessageText`, {
        chat_id: chatId,
        message_id: messageId,
        text,
      });
    } catch {
      // Ignored: expired edit or unchanged content
    }
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

async function processUserPrompt(params: {
  chatId: string | number;
  promptText: string;
  tokens: any;
  audioPayload?: any;
  imagePayload?: any;
  locationPayload?: { latitude: number; longitude: number } | null;
}): Promise<void> {
  const { chatId, promptText, tokens, audioPayload, imagePayload, locationPayload } = params;

  let loaderMessageId = await sendTelegramMessage(chatId, "⚡ Otis is thinking...");

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

    if (!finalReply || !finalReply.trim()) {
      finalReply = "✅ Action completed.";
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

    
   
   // 2. Resilient Visual Media Extractor (handles local paths & URLs regardless of Gemini tags)
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
      if (loaderMessageId) {
        await editTelegramMessage(chatId, loaderMessageId, finalReply);
      } else {
        await sendTelegramMessage(chatId, finalReply);
      }
    }
  } catch (err: any) {
    console.error("Agent Dispatch Error:", err.message);
    const errMsg = "⚠️ Otis encountered an issue executing this request.";
    if (loaderMessageId) {
      await editTelegramMessage(chatId, loaderMessageId, errMsg);
    } else {
      await sendTelegramMessage(chatId, errMsg);
    }
  }
}

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
      "✅ **Google Workspace Connected Successfully!**\n\nOtis now has access to your Gmail, Calendar, Drive, and Sheets. What would you like to do?"
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

  // 1. Handle Inline Button Clicks
// 1. Handle Inline Button Clicks
  if (body.callback_query) {
    const cq = body.callback_query;
    const data: string = cq.data || "";
    const messageId = cq.message?.message_id;
    const chatId = cq.message?.chat?.id;

    console.log(`\n🔘 [BUTTON CLICK DETECTED] callback_data: "${data}" from chat: ${chatId}`);

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
        await editTelegramMessage(chatId, messageId, "❌ Staged email draft was cancelled.");
      }
      return;
    }

       if (actionType === "revise") {
      pendingActions.delete(actionId);
      await answerCallbackQuery(cq.id, "Draft kept for revision.");
      if (messageId && chatId) {
        await editTelegramMessage(chatId, messageId, "✏️ **Draft held for edits.**\n\nSend your instructions (e.g. *\"Make it more casual\"*, *\"Change subject to Follow-up\"*, or *\"Attach an image of an invoice\"*), and Otis will update it.");
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
          await editTelegramMessage(chatId, messageId, replyResult);
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

  // 2. Incoming Messages
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
      await sendTelegramMessage(chatId, "👋 **Otis online.** Ready to assist with your workspace, tasks, and schedule.");
    }
    return;
  }

  if (!hasTokens) {
    await sendAuthPrompt(chatId);
    return;
  }

  let promptText = msg.text || msg.caption || "";
  let audioPayload: any = null;
  let imagePayload: any = null;
  let locationPayload: { latitude: number; longitude: number } | null = null;

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
    } catch (e: any) {
      console.error("Image download error:", e.message);
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

app.listen(PORT, () => {
  console.log(`🚀 Otis Node.js Gateway running on port ${PORT}`);
});