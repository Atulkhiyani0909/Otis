import fs from "fs";
import path from "path";
import crypto from "crypto";
import { config } from "./config.js";

export const TOKEN_FILE_PATH = path.resolve(process.cwd(), "tokens.json");
const ALGORITHM = "aes-256-gcm";

interface StoredTokens {
  access_token?: string | null;
  refresh_token?: string | null;
  scope?: string;
  token_type?: string | null;
  expiry_date?: number | null;
}

export function encrypt(text: string): string {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv(
    ALGORITHM,
    Buffer.from(config.TOKEN_ENCRYPTION_KEY),
    iv
  );
  let encrypted = cipher.update(text, "utf8", "hex");
  encrypted += cipher.final("hex");
  const authTag = cipher.getAuthTag().toString("hex");

  // Format: iv:authTag:encryptedData
  return `${iv.toString("hex")}:${authTag}:${encrypted}`;
}

export function decrypt(encryptedText: string): string {
  const [ivHex, authTagHex, encryptedData] = encryptedText.split(":");
  const decipher = crypto.createDecipheriv(
    ALGORITHM,
    Buffer.from(config.TOKEN_ENCRYPTION_KEY),
    Buffer.from(ivHex, "hex")
  );
  decipher.setAuthTag(Buffer.from(authTagHex, "hex"));
  let decrypted = decipher.update(encryptedData, "hex", "utf8");
  decrypted += decipher.final("utf8");
  return decrypted;
}

export function saveUserTokens(chatId: string, tokens: StoredTokens): void {
  let allTokens: Record<string, string> = {};
  if (fs.existsSync(TOKEN_FILE_PATH)) {
    try {
      allTokens = JSON.parse(fs.readFileSync(TOKEN_FILE_PATH, "utf8"));
    } catch {
      allTokens = {};
    }
  }

  const encryptedData = encrypt(JSON.stringify(tokens));
  allTokens[chatId] = encryptedData;
  fs.writeFileSync(TOKEN_FILE_PATH, JSON.stringify(allTokens, null, 2), "utf8");
}

export function getUserTokens(chatId: string): StoredTokens | null {
  if (!fs.existsSync(TOKEN_FILE_PATH)) return null;

  try {
    const raw = fs.readFileSync(TOKEN_FILE_PATH, "utf8");
    const allTokens = JSON.parse(raw);
    const encryptedData = allTokens[chatId];
    if (!encryptedData) return null;

    const decrypted = decrypt(encryptedData);
    return JSON.parse(decrypted);
  } catch (error) {
    console.error("Error reading or decrypting user tokens:", error);
    return null;
  }
}


export function deleteUserTokens(chatId: string): boolean {
  if (!fs.existsSync(TOKEN_FILE_PATH)) return false;

  try {
    const allTokens: Record<string, string> = JSON.parse(
      fs.readFileSync(TOKEN_FILE_PATH, "utf8")
    );
    if (!(chatId in allTokens)) return false;

    delete allTokens[chatId];
    fs.writeFileSync(TOKEN_FILE_PATH, JSON.stringify(allTokens, null, 2), "utf8");
    return true;
  } catch (error) {
    console.error("Error deleting user tokens:", error);
    return false;
  }
}