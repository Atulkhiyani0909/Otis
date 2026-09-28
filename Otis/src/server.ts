import express, { Request, Response } from "express";
import { config } from "./config.js";
import { oauth2Client, getAuthUrl } from "./googleAuth.js";
import { saveUserTokens, getUserTokens } from "./tokenStore.js";
import { success_response } from "./Reponse/website-reponse.js";
import axios from "axios";
import FormData from "form-data";
import { createParser } from "eventsource-parser";

const PYTHON_AGENT_URL = process.env.PYTHON_AGENT_URL || "http://127.0.0.1:8000/api/agent/dispatch";
const TELEGRAM_API_BASE = `https://api.telegram.org/bot${config.TELEGRAM_BOT_TOKEN}`;

const app = express();
app.use(express.json());

// ==========================================
// MARKDOWN CONVERSION HELPERS
// ==========================================
// LLM agents write standard Markdown (**bold**, # Header, - bullets, etc).
// Telegram's legacy "Markdown" parse_mode only understands *bold*, _italic_,
// `code`, and [text](url) — anything else either renders literally (stray
// ** characters visible to the user) or makes Telegram reject the message
// with a 400 "can't parse entities" error. These two helpers convert LLM
// Markdown into something Telegram can actually render, and provide a clean
// plain-text fallback (instead of sending raw unconverted markup) if the
// Markdown attempt still fails.

function toTelegramMarkdown(text: string): string {
  if (!text) return text;
  let out = text;

  // **bold** -> *bold*  (Telegram legacy bold is a single asterisk)
  out = out.replace(/\*\*(.+?)\*\*/g, "*$1*");

  // # Header / ## Header -> *Header* (bold line, since Telegram has no headers)
  out = out.replace(/^#{1,6}\s+(.*)$/gm, "*$1*");

  // "- item" or "* item" bullet markers -> "• item"
  out = out.replace(/^[ \t]*[-*]\s+/gm, "• ");

  // Collapse 3+ backtick code fences' language tag line (Telegram doesn't
  // render language hints, just leave the fence as-is; safe no-op if absent)
  return out;
}

function toPlainText(text: string): string {
  if (!text) return text;
  let out = text;

  out = out.replace(/\*\*(.+?)\*\*/g, "$1"); // bold
  out = out.replace(/\*(.+?)\*/g, "$1"); // bold (single)
  out = out.replace(/_(.+?)_/g, "$1"); // italic
  out = out.replace(/`{1,3}([^`]*)`{1,3}/g, "$1"); // inline/code block
  out = out.replace(/^#{1,6}\s+/gm, ""); // headers
  out = out.replace(/^[ \t]*[-*]\s+/gm, "• "); // bullets

  return out;
}

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
      text: toTelegramMarkdown(text),
      parse_mode: "Markdown",
    });
    return res.data?.result?.message_id ?? null;
  } catch (error: any) {
    // Fallback to clean plain text (not raw unconverted markup) if entities fail to parse
    try {
      const res = await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
        chat_id: chatId,
        text: toPlainText(text),
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
      text: toTelegramMarkdown(text),
      parse_mode: "Markdown",
    });
  } catch (error: any) {
    // Fallback to clean plain text if Markdown parsing fails
    try {
      await axios.post(`${TELEGRAM_API_BASE}/editMessageText`, {
        chat_id: chatId,
        message_id: messageId,
        text: toPlainText(text),
      });
    } catch (innerErr: any) {
      console.error("Failed to edit Telegram message:", innerErr.response?.data || innerErr.message);
    }
  }
}

// 5. Send photo to Telegram via URL
async function sendTelegramPhoto(chatId: string | number, photoUrl: string, caption?: string): Promise<void> {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/sendPhoto`, {
      chat_id: chatId,
      photo: photoUrl,
      caption: caption ? toTelegramMarkdown(caption) : "",
      parse_mode: "Markdown",
    });
  } catch (err: any) {
    console.error("Failed to send Telegram photo:", err.response?.data || err.message);
    // Fallback: retry without Markdown parse mode if entities failed
    try {
      await axios.post(`${TELEGRAM_API_BASE}/sendPhoto`, {
        chat_id: chatId,
        photo: photoUrl,
        caption: caption ? toPlainText(caption) : "",
      });
    } catch (innerErr: any) {
      console.error("Failed to send Telegram photo (fallback):", innerErr.response?.data || innerErr.message);
    }
  }
}

// 6. Send photo to Telegram from a base64 (or data URI) payload
async function sendTelegramBase64Photo(
  chatId: string | number,
  base64Data: string,
  caption?: string
): Promise<void> {
  try {
    // Strip a data URI prefix like "data:image/png;base64," if present
    const commaIndex = base64Data.indexOf(",");
    const cleanBase64 =
      base64Data.startsWith("data:") && commaIndex !== -1
        ? base64Data.slice(commaIndex + 1)
        : base64Data;

    const buffer = Buffer.from(cleanBase64, "base64");

    const form = new FormData();
    form.append("chat_id", String(chatId));
    if (caption) {
      form.append("caption", toTelegramMarkdown(caption));
      form.append("parse_mode", "Markdown");
    }
    form.append("photo", buffer, {
      filename: "image.png",
      contentType: "image/png",
    });

    await axios.post(`${TELEGRAM_API_BASE}/sendPhoto`, form, {
      headers: form.getHeaders(),
    });
  } catch (err: any) {
    console.error("Failed to send Telegram base64 photo:", err.response?.data || err.message);
  }
}

// Helper: Download a Telegram file by file_id and return base64
async function downloadTelegramFileAsBase64(fileId: string): Promise<string | null> {
  try {
    // 1. Get file path from Telegram
    const fileRes = await axios.get(`${TELEGRAM_API_BASE}/getFile?file_id=${fileId}`);
    const filePath = fileRes.data?.result?.file_path;
    if (!filePath) return null;

    // 2. Download the actual binary file
    const downloadUrl = `https://api.telegram.org/file/bot${config.TELEGRAM_BOT_TOKEN}/${filePath}`;
    const audioRes = await axios.get(downloadUrl, { responseType: "arraybuffer" });

    // 3. Convert buffer to base64
    return Buffer.from(audioRes.data).toString("base64");
  } catch (err: any) {
    console.error("Failed to download Telegram voice file:", err.response?.data || err.message);
    return null;
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
      "✅ *Otis has linked with Google Workspace!*"
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

  res.sendStatus(200);

  const update = req.body;
  const message = update?.message;
  if (!message) return;

  const hasText = Boolean(message.text);
  const hasLocation = Boolean(message.location);
  const hasVoice = Boolean(message.voice);
  const hasPhoto = Boolean(message.photo && message.photo.length > 0);

  if (!hasText && !hasLocation && !hasVoice && !hasPhoto) return;

  const senderId = String(message.from?.id);
  const chatId = message.chat.id;

  if (senderId !== config.TELEGRAM_ALLOWED_USER_ID) {
    await sendTelegramMessage(chatId, "⛔ *Access Denied:* Otis is a private executive assistant.");
    return;
  }

  const tokens = getUserTokens(senderId);
  if (!tokens) {
    const loginUrl = `${config.SERVER_PUBLIC_URL}/auth/google/login?chatId=${chatId}`;
    await sendTelegramMessage(chatId, `⚠️ *Google authorization required.*\n\n🔗 [Link Google Workspace](${loginUrl})`);
    return;
  }

  // 1. Initial status indicator
  let loaderText = "⏳ *Thinking...*";
  if (hasPhoto) loaderText = "🖼️ *Analyzing your image...*";
  else if (hasVoice) loaderText = "🎙️ *Listening to your voice note...*";
  else if (hasLocation) loaderText = "📍 *Reading your GPS coordinates...*";

  const loaderMessageId = await sendTelegramMessage(chatId, loaderText);
  const stopTyping = startTypingHeartbeat(chatId);

  try {
    let promptText = "";
    let audioPayload: any = null;
    let imagePayload: any = null;

    if (hasPhoto) {
      const highestResPhoto = message.photo[message.photo.length - 1];
      const base64Img = await downloadTelegramFileAsBase64(highestResPhoto.file_id);
      imagePayload = { data: base64Img, mime_type: "image/jpeg" };
      promptText = message.caption ? message.caption.trim() : "Analyze this image and execute any relevant tools.";
    } else if (hasVoice) {
      const base64Audio = await downloadTelegramFileAsBase64(message.voice.file_id);
      audioPayload = { data: base64Audio, mime_type: message.voice.mime_type || "audio/ogg" };
      promptText = "The user sent a voice message. Execute requested actions and reply.";
    } else if (hasLocation) {
      promptText = `Location: lat=${message.location.latitude}, lon=${message.location.longitude}. Check weather.`;
    } else {
      promptText = message.text.trim();
    }

    // 2. Open an HTTP stream to the FastAPI SSE endpoint
    const streamResponse = await axios.post(
      `${PYTHON_AGENT_URL}-stream`, // http://127.0.0.1:8000/api/agent/dispatch-stream
      {
        prompt: promptText,
        chat_id: String(chatId),
        google_tokens: tokens,
        audio: audioPayload,
        image: imagePayload,
      },
      { responseType: "stream" ,
        timeout:200_000
      }
    );

    let finalReply = "";
    let lastStatusText = loaderText;
    let lastEditTimestamp = 0;
    const RATE_LIMIT_MS = 1200; // Telegram message edit cooldown (avoids 429 errors)

    // Helper to safely update Telegram status with rate limiting
    const updateStatusSafe = async (newText: string) => {
      const now = Date.now();
      if (newText !== lastStatusText && now - lastEditTimestamp >= RATE_LIMIT_MS) {
        lastStatusText = newText;
        lastEditTimestamp = now;
        if (loaderMessageId) {
          await editTelegramMessage(chatId, loaderMessageId, newText).catch(() => {});
        }
      }
    };

    // 3. SSE stream parser
    const parser = createParser({
      onEvent(event: any) {
        try {
          const payload = JSON.parse(event.data);

          // Tool execution event (e.g. "🔍 Searching the web...")
          if (payload.type === "status" && payload.message) {
            updateStatusSafe(payload.message);
          }

          // Final completion event
          if (payload.type === "final" && payload.response) {
            finalReply = payload.response;
          }
        } catch (err) {
          // Skip unparseable chunks
        }
      },
    });

    // Feed incoming HTTP chunks into the SSE parser (single listener)
    streamResponse.data.on("data", (chunk: Buffer) => {
      parser.feed(chunk.toString("utf-8"));
    });

    // Wait until the stream completes (single listener pair)
    await new Promise((resolve, reject) => {
      streamResponse.data.on("end", resolve);
      streamResponse.data.on("error", reject);
    });

    // Stop the typing heartbeat the moment we're done waiting on the agent —
    // not after we've also finished formatting/sending the reply. Telegram's
    // "typing..." bubble only auto-expires up to 5s after the *last* chat
    // action sent, and editing a message does not clear it early, so any
    // heartbeat tick fired during the final send/edit calls below would
    // leave the bubble visible for several seconds after the answer appears.
    stopTyping();

    if (!finalReply) {
      finalReply = "Action completed, but no textual summary was returned.";
    }

    // 4. Handle output (Base64 image, Image URL, or Text)
    const base64Match = finalReply.match(/\[IMAGE_BASE64:\s*([^\]]+)\]/);
    const imgMatch = finalReply.match(/\[IMAGE_URL:\s*(https?:\/\/[^\s\]]+)\]/);

    if (base64Match) {
      const dataUri = base64Match[1];
      const cleanReply = finalReply.replace(/\[IMAGE_BASE64:\s*[^\]]+\]/, "").trim();
      if (loaderMessageId) await editTelegramMessage(chatId, loaderMessageId, "✅ *Generated:*");
      await sendTelegramBase64Photo(chatId, dataUri, cleanReply);
    } else if (imgMatch) {
      const photoUrl = imgMatch[1];
      const cleanReply = finalReply.replace(/\[IMAGE_URL:\s*https?:\/\/[^\s\]]+\]/, "").trim();
      if (loaderMessageId) await editTelegramMessage(chatId, loaderMessageId, "📸 *Here is what I found:*");
      await sendTelegramPhoto(chatId, photoUrl, cleanReply);
    } else {
      // Deliver the final generated Markdown response
      if (loaderMessageId) {
        await editTelegramMessage(chatId, loaderMessageId, finalReply);
      } else {
        await sendTelegramMessage(chatId, finalReply);
      }
    }
  } catch (error: any) {
    // Stop the heartbeat immediately on error too, before spending time
    // sending the error notice, for the same reason as above.
    stopTyping();
    console.error("Agent dispatch error:", error.response?.data || error.message);
    const errorNotice = "⚠️ *Otis encountered an issue* processing your request.";
    if (loaderMessageId) {
      await editTelegramMessage(chatId, loaderMessageId, errorNotice);
    } else {
      await sendTelegramMessage(chatId, errorNotice);
    }
  } finally {
    // Safety net in case an early return/throw skipped both explicit calls
    // above. clearInterval on an already-cleared interval is a harmless no-op.
    stopTyping();
  }
});

app.listen(config.PORT, () => {
  console.log(`Otis Gateway active on port ${config.PORT}`);
});