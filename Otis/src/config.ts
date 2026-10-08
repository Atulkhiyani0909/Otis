import dotenv from 'dotenv'
import {z} from 'zod'

dotenv.config();

const envSchema = z.object({
  PORT: z.string().default("3000").transform((val) => parseInt(val, 10)),
  NODE_ENV: z.enum(["development", "production", "test"]).default("development"),
  TELEGRAM_BOT_TOKEN: z.string().min(1, "TELEGRAM_BOT_TOKEN is required"),
  // TELEGRAM_ALLOWED_USER_ID: z.string().min(1, "TELEGRAM_ALLOWED_USER_ID is required"),
  TELEGRAM_WEBHOOK_SECRET: z.string().min(16, "TELEGRAM_WEBHOOK_SECRET must be at least 16 chars"),
  SERVER_PUBLIC_URL: z.string().url("SERVER_PUBLIC_URL must be a valid URL"),
  GOOGLE_CLIENT_ID: z.string().min(1, "GOOGLE_CLIENT_ID is required"),
  GOOGLE_CLIENT_SECRET: z.string().min(1, "GOOGLE_CLIENT_SECRET is required"),
  GOOGLE_REDIRECT_URI: z.string().url("GOOGLE_REDIRECT_URI must be a valid URL"),
  TOKEN_ENCRYPTION_KEY: z.string().length(32, "TOKEN_ENCRYPTION_KEY must be exactly 32 chars")
});

export const config = envSchema.parse(process.env);