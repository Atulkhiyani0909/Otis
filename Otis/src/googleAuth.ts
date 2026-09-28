import { google } from "googleapis";
import { config } from "./config.js";

export const oauth2Client = new google.auth.OAuth2(
  config.GOOGLE_CLIENT_ID,
  config.GOOGLE_CLIENT_SECRET,
  config.GOOGLE_REDIRECT_URI
);


export const SCOPES = [
  "https://www.googleapis.com/auth/calendar",
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/gmail.send",
  "https://www.googleapis.com/auth/contacts.readonly",
  "https://www.googleapis.com/auth/tasks",
  "https://www.googleapis.com/auth/drive.readonly",
  "https://www.googleapis.com/auth/spreadsheets"
];

export function getAuthUrl(telegramChatId: string): string {
  return oauth2Client.generateAuthUrl({
    access_type: "offline", // Ensures Google returns a refresh_token
    prompt: "consent",      // Forces consent screen so refresh_token is re-issued
    scope: SCOPES,
    state: telegramChatId,  // Transports the chat ID through the OAuth handshake safely
  });
}