import axios from "axios";
import { config } from "./config.js";

async function registerWebhook() {
  const webhookUrl = `${config.SERVER_PUBLIC_URL}/webhook/telegram`;
  const endpoint = `https://api.telegram.org/bot${config.TELEGRAM_BOT_TOKEN}/setWebhook`;

  try {
    const response = await axios.post(endpoint, {
      url: webhookUrl,
      secret_token: config.TELEGRAM_WEBHOOK_SECRET,
      allowed_updates: ["message"],
      drop_pending_updates: true, 
    });

    console.log("Telegram Webhook Registration Response:", response.data);
  } catch (error: any) {
    console.error("Failed to set webhook:", error.response?.data || error.message);
    process.exit(1);
  }
}

registerWebhook();