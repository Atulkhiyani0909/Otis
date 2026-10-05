import express, { Request, Response } from "express";
import axios from "axios";
import dotenv from "dotenv";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import FormData from "form-data";
import { createParser, type EventSourceMessage } from "eventsource-parser";

dotenv.config();

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || "";
const TELEGRAM_API_BASE = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}`;
const PYTHON_AGENT_URL = process.env.PYTHON_AGENT_URL || "http://127.0.0.1:8000/api/agent/dispatch";

// Staging for inline human-in-the-loop approvals
const pendingActions = new Map<string, { chatId: string | number; actionData: any; tokens: any }>();

// ---------------------------------------------------------------------------
// Decryption & Token Retrieval Engine
// ---------------------------------------------------------------------------

function decryptTokenString(cipherText: string): any {
  try {
    const rawKey = process.env.ENCRYPTION_KEY || process.env.TOKEN_SECRET || process.env.JWT_SECRET || "";
    if (!rawKey) {
      console.warn("[AUTH WARNING] Missing ENCRYPTION_KEY in .env, attempting direct parse.");
      return null;
    }

    const key = rawKey.length === 64
      ? Buffer.from(rawKey, "hex")
      : crypto.createHash("sha256").update(rawKey).digest();

    const parts = cipherText.split(":");
    if (parts.length !== 3) return null;

    const [ivHex, authTagHex, encryptedHex] = parts;
    const iv = Buffer.from(ivHex, "hex");
    const authTag = Buffer.from(authTagHex, "hex");
    const decipher = crypto.createDecipheriv("aes-256-gcm", key, iv);

    decipher.setAuthTag(authTag);
    let decrypted = decipher.update(encryptedHex, "hex", "utf8");
    decrypted += decipher.final("utf8");

    return JSON.parse(decrypted);
  } catch (err: any) {
    console.error("[AUTH DECRYPT ERROR]:", err.message);
    return null;
  }
}

function getStoredTokens(chatId: string | number): any {
  try {
    const candidatePaths = [
      path.resolve(process.cwd(), "tokens.json"),
      path.resolve(process.cwd(), "..", "tokens.json"),
      path.resolve(__dirname, "..", "tokens.json"),
      path.resolve(__dirname, "tokens.json"),
    ];

    let tokenFilePath = "";
    for (const p of candidatePaths) {
      if (fs.existsSync(p)) {
        tokenFilePath = p;
        break;
      }
    }

    if (!tokenFilePath) {
      console.warn("⚠️ tokens.json not found on disk.");
      return {};
    }

    const raw = fs.readFileSync(tokenFilePath, "utf-8");
    const parsed = JSON.parse(raw);
    const entry = parsed[String(chatId)] || parsed;

    if (typeof entry === "string" && entry.includes(":")) {
      const decrypted = decryptTokenString(entry);
      if (decrypted) return decrypted;
    }

    if (typeof entry === "object" && entry !== null) {
      return entry;
    }

    return {};
  } catch (err: any) {
    console.error("Failed to read tokens.json:", err.message);
    return {};
  }
}

// ---------------------------------------------------------------------------
// Telegram Message & Photo Helpers
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
  try {
    await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
      chat_id: chatId,
      text: sanitizeMarkdown(text),
      parse_mode: "Markdown",
      reply_markup: {
        inline_keyboard: [
          [
            { text: "✅ Send Email", callback_data: `confirm:${actionId}` },
            { text: "❌ Cancel", callback_data: `cancel:${actionId}` },
          ],
        ],
      },
    });
  } catch (err: any) {
    console.error("Failed to send approval keyboard:", err.response?.data || err.message);
  }
}

async function answerCallbackQuery(callbackQueryId: string, text?: string): Promise<void> {
  await axios.post(`${TELEGRAM_API_BASE}/answerCallbackQuery`, {
    callback_query_id: callbackQueryId,
    text,
  }).catch(() => {});
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
}else if (payload.type === "final" && payload.response) {
            finalReply = payload.response;
          }
        } catch {
          // Ignore partial or non-JSON chunks
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

    // 2. Process Visual Media
    const base64Match = finalReply.match(/\[IMAGE_BASE64:\s*([^\]]+)\]/);
    const imgMatch = finalReply.match(/\[IMAGE_URL:\s*(https?:\/\/[^\s\]]+)\]/);

    if (base64Match) {
      const dataUri = base64Match[1];
      const cleanReply = finalReply.replace(/\[IMAGE_BASE64:\s*[^\]]+\]/, "").trim();
      if (loaderMessageId) await editTelegramMessage(chatId, loaderMessageId, "✅ Generated visual:");
      await sendTelegramBase64Photo(chatId, dataUri, cleanReply);
    } else if (imgMatch) {
      const photoUrl = imgMatch[1];
      const cleanReply = finalReply.replace(/\[IMAGE_URL:\s*https?:\/\/[^\s\]]+\]/, "").trim();
      if (loaderMessageId) await editTelegramMessage(chatId, loaderMessageId, "📸 Image retrieved:");
      await sendTelegramPhoto(chatId, photoUrl, cleanReply);
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
// Telegram Webhook Handler
// ---------------------------------------------------------------------------

app.post("/webhook/telegram", async (req: Request, res: Response) => {
  res.sendStatus(200);

  const body = req.body;

  // 1. Interactive Button Callback Queries
  if (body.callback_query) {
    const cq = body.callback_query;
    const data: string = cq.data || "";
    const messageId = cq.message?.message_id;
    const chatId = cq.message?.chat?.id;

    const [actionType, actionId] = data.split(":");
    const pending = pendingActions.get(actionId);

    if (!pending) {
      await answerCallbackQuery(cq.id, "This approval request has expired.");
      return;
    }

    if (actionType === "cancel") {
      pendingActions.delete(actionId);
      await answerCallbackQuery(cq.id, "Action cancelled.");
      if (messageId && chatId) {
        await editTelegramMessage(chatId, messageId, "❌ Staged email draft was cancelled.");
      }
      return;
    }

    if (actionType === "confirm") {
      await answerCallbackQuery(cq.id, "Sending email...");
      if (messageId && chatId) {
        await editTelegramMessage(chatId, messageId, "🚀 Sending email...");
      }

      try {
        const response = await axios.post("http://127.0.0.1:8000/api/agent/confirm-action", {
          chat_id: chatId,
          google_tokens: pending.tokens,
          action_data: pending.actionData,
        });

        pendingActions.delete(actionId);
        if (messageId && chatId) {
          await editTelegramMessage(chatId, messageId, response.data.result || "✅ Email dispatched.");
        }
      } catch (err: any) {
        if (messageId && chatId) {
          await editTelegramMessage(chatId, messageId, `❌ Failed to dispatch email: ${err.message}`);
        }
      }
    }
    return;
  }

  // 2. Incoming Messages
  if (!body.message) return;

  const msg = body.message;
  const chatId = msg.chat.id;

  const tokens = getStoredTokens(chatId);

  if (msg.text === "/start") {
    await sendTelegramMessage(chatId, "👋 **Otis online.** Ready to assist with your workspace, tasks, and schedule.");
    return;
  }

  let promptText = msg.text || msg.caption || "";
  let audioPayload: any = null;
  let imagePayload: any = null;
  let locationPayload: { latitude: number; longitude: number } | null = null;

  // Real-Time Location Pins
  if (msg.location) {
    locationPayload = {
      latitude: msg.location.latitude,
      longitude: msg.location.longitude,
    };
    if (!promptText) {
      promptText = `I have shared my current coordinates: Latitude ${msg.location.latitude}, Longitude ${msg.location.longitude}. Use these coordinates for any local queries, navigation, or weather requests.`;
    }
  }

  // Audio / Voice Memos
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

  // Image Attachments
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