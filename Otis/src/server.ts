import express, { Request, Response } from "express";
import { config } from "./config.js";
import { oauth2Client, getAuthUrl } from "./googleAuth.js";
import { saveUserTokens, getUserTokens } from "./tokenStore.js";
import { success_response } from "./Reponse/website-reponse.js";
import axios from "axios";

const PYTHON_AGENT_URL = process.env.PYTHON_AGENT_URL || "http://127.0.0.1:8000/api/agent/dispatch";
const TELEGRAM_API_BASE = `https://api.telegram.org/bot${config.TELEGRAM_BOT_TOKEN}`;

const app = express();
app.use(express.json());

// ==========================================
// TELEGRAM UI & LOADER HELPERS
// ==========================================

// 1. Send native chat action (e.g. typing)
async function sendChatAction(chatId: string | number, action = "typing"): Promise<void> {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/sendChatAction`, {
      chat_id: chatId,
      action: action,
    });
  } catch (err: any) {
    console.error("Failed to send chat action:", err.response?.data || err.message);
  }
}

// 2. Typing heartbeat to keep the indicator active throughout long tool calls
function startTypingHeartbeat(chatId: string | number): () => void {
  sendChatAction(chatId, "typing");
  const intervalId = setInterval(() => {
    sendChatAction(chatId, "typing");
  }, 4000);

  return () => clearInterval(intervalId);
}

// 3. Send an initial Telegram message (returns message_id for in-place editing)
async function sendTelegramMessage(chatId: string | number, text: string): Promise<number | null> {
  try {
    const res = await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
      chat_id: chatId,
      text: text,
      parse_mode: "Markdown",
    });
    return res.data?.result?.message_id ?? null;
  } catch (error: any) {
    // Fallback without Markdown if entities fail to parse
    try {
      const res = await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
        chat_id: chatId,
        text: text,
      });
      return res.data?.result?.message_id ?? null;
    } catch (innerErr: any) {
      console.error("Failed to send Telegram message:", innerErr.response?.data || innerErr.message);
      return null;
    }
  }
}

// 4. In-place edit of an existing message (used to replace the loader with the answer)
async function editTelegramMessage(
  chatId: string | number,
  messageId: number,
  text: string
): Promise<void> {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/editMessageText`, {
      chat_id: chatId,
      message_id: messageId,
      text: text,
      parse_mode: "Markdown",
    });
  } catch (error: any) {
    // Fallback to plain text if Markdown parsing fails
    try {
      await axios.post(`${TELEGRAM_API_BASE}/editMessageText`, {
        chat_id: chatId,
        message_id: messageId,
        text: text,
      });
    } catch (innerErr: any) {
      console.error("Failed to edit Telegram message:", innerErr.response?.data || innerErr.message);
    }
  }
}

// ==========================================
// HEALTH CHECK
// ==========================================
app.get("/health", (req: Request, res: Response) => {
  res.status(200).json({ status: "healthy", timestamp: new Date().toISOString() });
});

// ==========================================
// 1. GOOGLE OAUTH 2.0 FLOW
// ==========================================
app.get("/auth/google/login", (req: Request, res: Response) => {
  const chatId = req.query.chatId as string;
  if (!chatId || chatId !== config.TELEGRAM_ALLOWED_USER_ID) {
    res.status(401).send("Unauthorized chat ID");
    return;
  }

  const url = getAuthUrl(chatId);
  res.redirect(url);
});

app.get("/auth/google/callback", async (req: Request, res: Response): Promise<void> => {
  const { code, state } = req.query;

  if (!code || !state || typeof state !== "string" || typeof code !== "string") {
    res.status(400).send("Invalid OAuth callback parameters.");
    return;
  }

  const telegramChatId = state;

  try {
    const { tokens } = await oauth2Client.getToken(code);
    saveUserTokens(telegramChatId, tokens);

    await sendTelegramMessage(
      telegramChatId,
      "✅ *Otis has linked with Google Workspace!*\n\nI can now access your Gmail, Google Calendar, and Contacts."
    );

    res.send(success_response);
  } catch (error: any) {
    console.error("OAuth Exchange Error:", error.response?.data || error.message);
    res.status(500).send("Failed to exchange code for tokens. Check server logs.");
  }
});

// ==========================================
// 2. TELEGRAM WEBHOOK ROUTE
// ==========================================
app.post("/webhook/telegram", async (req: Request, res: Response): Promise<void> => {
  const incomingSecret = req.headers["x-telegram-bot-api-secret-token"];
  if (incomingSecret !== config.TELEGRAM_WEBHOOK_SECRET) {
    res.status(403).json({ error: "Forbidden" });
    return;
  }

  // Instant ACK back to Telegram
  res.sendStatus(200);

  const update = req.body;
  const message = update?.message;
  if (!message || !message.text) return;

  const senderId = String(message.from?.id);
  const chatId = message.chat.id;
  const text = message.text.trim();

  // Zero-trust user verification
  if (senderId !== config.TELEGRAM_ALLOWED_USER_ID) {
    await sendTelegramMessage(chatId, "⛔ *Access Denied:* Otis is a private executive assistant.");
    return;
  }

  // Check if Google Workspace is connected
  const tokens = getUserTokens(senderId);

  if (text === "/start") {
    if (!tokens) {
      const loginUrl = `${config.SERVER_PUBLIC_URL}/auth/google/login?chatId=${chatId}`;
      await sendTelegramMessage(
        chatId,
        `👋 *Otis ready.*\n\nLink your Google Workspace to enable calendar, email, and contacts:\n\n🔗 [Link Google Workspace](${loginUrl})`
      );
    } else {
      await sendTelegramMessage(chatId, "👋 *Otis online and ready.* What are we tackling?");
    }
    return;
  }

  if (!tokens) {
    const loginUrl = `${config.SERVER_PUBLIC_URL}/auth/google/login?chatId=${chatId}`;
    await sendTelegramMessage(
      chatId,
      `⚠️ *Google authorization required.*\nPlease authenticate first:\n\n🔗 [Link Google Workspace](${loginUrl})`
    );
    return;
  }

  // 1. Post immediate in-chat loader message
  const loaderMessageId = await sendTelegramMessage(chatId, "⏳ *Thinking...*");

  // 2. Start continuous native typing indicator
  const stopTyping = startTypingHeartbeat(chatId);

  try {
    // 3. Dispatch to Python LangGraph Agent
    const agentResponse = await axios.post(PYTHON_AGENT_URL, {
      prompt: text,
      chat_id: String(chatId),
      google_tokens: tokens,
    });

    let replyText = agentResponse.data?.response || "Task completed.";

    // Defensive parsing for Gemini block objects/arrays
    if (Array.isArray(replyText)) {
      replyText = replyText
        .filter((item: any) => item?.type === "text")
        .map((item: any) => item.text)
        .join("\n");
    } else if (typeof replyText === "object" && replyText !== null) {
      replyText = replyText.text || JSON.stringify(replyText);
    }

    // 4. In-place edit: replace loader with final response
    if (loaderMessageId) {
      await editTelegramMessage(chatId, loaderMessageId, replyText);
    } else {
      await sendTelegramMessage(chatId, replyText);
    }
  } catch (error: any) {
    console.error("Agent dispatch error:", error.response?.data || error.message);
    const errorNotice = "⚠️ *Otis encountered an issue* communicating with the AI service.";

    if (loaderMessageId) {
      await editTelegramMessage(chatId, loaderMessageId, errorNotice);
    } else {
      await sendTelegramMessage(chatId, errorNotice);
    }
  } finally {
    // 5. Always stop typing heartbeat
    stopTyping();
  }
});

app.listen(config.PORT, () => {
  console.log(`Otis Gateway active on port ${config.PORT}`);
});