import { google } from "googleapis";
import dotenv from "dotenv";

dotenv.config();

export const SCOPES = [
  // Gmail Permissions
  "https://www.googleapis.com/auth/gmail.send",
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/gmail.modify",

  // Calendar Permissions
  "https://www.googleapis.com/auth/calendar",

  // Contacts & Tasks Permissions
  "https://www.googleapis.com/auth/contacts.readonly",
  "https://www.googleapis.com/auth/tasks",

  // Drive & Sheets Permissions
  "https://www.googleapis.com/auth/drive.readonly",
  "https://www.googleapis.com/auth/spreadsheets",
];

export const oauth2Client = new google.auth.OAuth2(
  process.env.GOOGLE_CLIENT_ID,
  process.env.GOOGLE_CLIENT_SECRET,
  process.env.GOOGLE_REDIRECT_URI || "http://localhost:3000/auth/google/callback"
);

/**
 * Generates the Google OAuth consent URL.
 * - access_type: "offline" ensures Google provides a refresh_token.
 * - prompt: "consent" forces Google to reissue the refresh_token even on re-login.
 * - state: preserves the user's Telegram chatId across the OAuth redirection.
 */
export function getAuthUrl(telegramChatId: string | number): string {
  return oauth2Client.generateAuthUrl({
    access_type: "offline",
    prompt: "consent",
    scope: SCOPES,
    state: String(telegramChatId),
  });
}

/**
 * Exchanges the authorization code received from the callback redirect for OAuth tokens.
 */
export async function getTokensFromCode(code: string) {
  const { tokens } = await oauth2Client.getToken(code);
  return tokens;
}