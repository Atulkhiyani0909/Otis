import axios from "axios";
import { config } from "./config.js";

const TELEGRAM_API_BASE = `https://api.telegram.org/bot${config.TELEGRAM_BOT_TOKEN}`;

export async function sendTelegramMessage(chatId: string | number, text: string) {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/sendMessage`, {
      chat_id: chatId,
      text: text,
      parse_mode: "Markdown",
    });
  } catch (error: any) {
    console.error("Failed to send Telegram message:", error.response?.data || error.message);
  }
}

export async function sendTypingAction(chatId: string | number) {
  try {
    await axios.post(`${TELEGRAM_API_BASE}/sendChatAction`, {
      chat_id: chatId,
      action: "typing",
    });
  } catch (error: any) {
    console.error("Failed to send typing action:", error.response?.data || error.message);
  }
}