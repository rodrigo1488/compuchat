import Redis from "ioredis";
import { REDIS_URI_CONNECTION } from "../config/redis";
import { logger } from "../utils/logger";
import * as crypto from "crypto";

const redis = REDIS_URI_CONNECTION
  ? new Redis(REDIS_URI_CONNECTION)
  : new Redis({ host: "127.0.0.1", port: 6379 });

redis.on("error", (err: NodeJS.ErrnoException) => {
  logger.warn(`[redis/cache] ${err.code || ""} ${err.message}`);
});

function encryptParams(params: any) {
  const str = JSON.stringify(params);
  return crypto.createHash("sha256").update(str).digest("base64");
}

export function setFromParams(
  key: string,
  params: any,
  value: string,
  option?: string,
  optionValue?: string | number
) {
  const finalKey = `${key}:${encryptParams(params)}`;
  if (option !== undefined && optionValue !== undefined) {
    return set(finalKey, value, option, optionValue);
  }
  return set(finalKey, value);
}

export function getFromParams(key: string, params: any) {
  const finalKey = `${key}:${encryptParams(params)}`;
  return get(finalKey);
}

export function delFromParams(key: string, params: any) {
  const finalKey = `${key}:${encryptParams(params)}`;
  return del(finalKey);
}

export function set(
  key: string,
  value: string,
  option?: string,
  optionValue?: string | number
) {
  if (option !== undefined && optionValue !== undefined) {
    return redis.set(key, value, option as any, optionValue as any);
  }

  return redis.set(key, value);
}

export function get(key: string) {
  return redis.get(key);
}

export function getKeys(pattern: string) {
  return redis.keys(pattern);
}

export function del(key: string) {
  return redis.del(key);
}

export function delFromPattern(pattern: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const stream = redis.scanStream({ match: pattern, count: 200 });
    const keys: string[] = [];

    stream.on("data", (batch: string[]) => {
      if (batch?.length) keys.push(...batch);
    });

    stream.on("end", () => {
      const unlinkInChunks = async () => {
        for (let i = 0; i < keys.length; i += 200) {
          const slice = keys.slice(i, i + 200);
          if (slice.length) await redis.unlink(...slice);
        }
      };

      unlinkInChunks().then(() => resolve()).catch(reject);
    });

    stream.on("error", reject);
  });
}

export const cacheLayer = {
  set,
  setFromParams,
  get,
  getFromParams,
  getKeys,
  del,
  delFromParams,
  delFromPattern
};
