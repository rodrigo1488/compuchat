import * as crypto from "crypto";
import { cacheLayer } from "./cache";
import { logger } from "../utils/logger";

export type CacheNamespace =
  | "dashboard"
  | "tickets"
  | "contacts"
  | "settings"
  | "modules"
  | "users"
  | "company"
  | "catalog"
  | "queues";

export const CACHE_TTL = {
  live: 30,
  warm: 90,
  list: 60,
  inbox: 15,
  settings: 120,
  modules: 600,
  company: 600,
  historical: 300,
  external: 300,
  user: 120,
  super: 300
} as const;

const envTrue = (value: string | undefined): boolean =>
  value === "true" || value === "1";

export const isCacheLoggingEnabled = (): boolean =>
  process.env.NODE_ENV !== "production" || envTrue(process.env.CACHE_LOG_ENABLED);

export const isCacheEnabled = (): boolean =>
  envTrue(process.env.CACHE_ENABLED);

/** Cache em memória do processo. Ligado por padrão; desligue com HOT_CACHE_ENABLED=false. */
export const isHotCacheEnabled = (): boolean => {
  const flag = process.env.HOT_CACHE_ENABLED;
  if (flag === undefined) return true;
  return envTrue(flag);
};

export const isCacheNamespaceEnabled = (namespace: CacheNamespace): boolean => {
  if (!isCacheEnabled()) return false;
  const flag = process.env[`CACHE_${namespace.toUpperCase()}`];
  if (flag === undefined) return true;
  return envTrue(flag);
};

const hashParams = (params: unknown): string => {
  const str = JSON.stringify(params ?? {});
  return crypto.createHash("sha256").update(str).digest("base64url").slice(0, 16);
};

export const buildKey = (
  namespace: CacheNamespace,
  companyId: number | string | null,
  suffix: string,
  params?: unknown
): string => {
  const companyPart =
    companyId === null || companyId === undefined ? "global" : String(companyId);
  const paramsPart = params !== undefined ? `:${hashParams(params)}` : "";
  return `cc:${companyPart}:${namespace}:${suffix}${paramsPart}`;
};

export const buildUserKey = (
  userId: number | string,
  tokenVersion: number,
  suffix = "profile"
): string => `cc:user:${userId}:v${tokenVersion}:${suffix}`;

let lastHitMissLog = 0;
const HIT_MISS_LOG_INTERVAL_MS = 5000;

const MAX_HOT_ENTRIES = 400;
const MAX_STALE_EXTRA_MS = 60_000;

type HotEntry = {
  json: string;
  freshUntil: number;
  staleUntil: number;
};

const hotStore = new Map<string, HotEntry>();
const inflight = new Map<string, Promise<unknown>>();
const prefixEpoch = new Map<string, number>();

const bumpPrefix = (pattern: string): void => {
  const star = pattern.indexOf("*");
  const prefix = star === -1 ? pattern : pattern.slice(0, star);
  prefixEpoch.set(prefix, (prefixEpoch.get(prefix) || 0) + 1);
};

const epochOf = (key: string): number => {
  let epoch = 0;
  for (const [prefix, value] of prefixEpoch) {
    if (key.startsWith(prefix)) epoch += value;
  }
  return epoch;
};

const globToRegExp = (pattern: string): RegExp => {
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
  return new RegExp(`^${escaped}$`);
};

const clearHotKeys = (pattern: string): void => {
  const matcher = globToRegExp(pattern);
  for (const key of hotStore.keys()) {
    if (matcher.test(key)) hotStore.delete(key);
  }
};

const rememberHot = (key: string, json: string, ttlSeconds: number): void => {
  if (!isHotCacheEnabled()) return;
  const now = Date.now();
  const freshMs = Math.max(1, ttlSeconds) * 1000;
  if (hotStore.has(key)) hotStore.delete(key);
  hotStore.set(key, {
    json,
    freshUntil: now + freshMs,
    staleUntil: now + freshMs + Math.min(freshMs, MAX_STALE_EXTRA_MS)
  });
  while (hotStore.size > MAX_HOT_ENTRIES) {
    const oldest = hotStore.keys().next().value;
    if (oldest === undefined) break;
    hotStore.delete(oldest);
  }
};

const readHot = (key: string): { value: unknown; fresh: boolean } | null => {
  if (!isHotCacheEnabled()) return null;
  const entry = hotStore.get(key);
  if (!entry) return null;
  const now = Date.now();
  if (now > entry.staleUntil) {
    hotStore.delete(key);
    return null;
  }
  hotStore.delete(key);
  hotStore.set(key, entry);
  return {
    value: JSON.parse(entry.json),
    fresh: now <= entry.freshUntil
  };
};

const serialize = (value: unknown): string | null => {
  try {
    const json = JSON.stringify(value ?? null);
    return typeof json === "string" ? json : null;
  } catch (err: any) {
    logger.warn(`[appCache] serialize failed: ${err?.message || err}`);
    return null;
  }
};

const logCacheEvent = (
  event: "HIT" | "MISS" | "SKIP" | "ERROR",
  key: string,
  namespace: CacheNamespace,
  durationMs?: number
): void => {
  if (!isCacheLoggingEnabled()) return;

  if (process.env.NODE_ENV === "production") {
    const now = Date.now();
    if (now - lastHitMissLog < HIT_MISS_LOG_INTERVAL_MS) return;
    lastHitMissLog = now;
  }

  logger.debug({
    msg: `[appCache] ${event}`,
    namespace,
    key,
    durationMs
  });
};

const storeValue = async <T>(
  key: string,
  ttlSeconds: number,
  value: T,
  namespace: CacheNamespace
): Promise<T> => {
  const json = serialize(value);
  if (!json) return value;

  rememberHot(key, json, ttlSeconds);

  if (isCacheNamespaceEnabled(namespace)) {
    try {
      await cacheLayer.set(key, json, "EX", ttlSeconds);
    } catch (err: any) {
      logger.warn(`[appCache] set failed for ${key}: ${err?.message || err}`);
    }
  }

  return JSON.parse(json) as T;
};

const loadFresh = <T>(
  key: string,
  ttlSeconds: number,
  fetchFn: () => Promise<T>,
  namespace: CacheNamespace
): Promise<T> => {
  const existing = inflight.get(key);
  if (existing) return existing as Promise<T>;

  const promise = (async () => {
    const seenEpoch = epochOf(key);
    const value = await fetchFn();
    if (epochOf(key) !== seenEpoch) {
      return value;
    }
    return storeValue(key, ttlSeconds, value, namespace);
  })().finally(() => {
    inflight.delete(key);
  });

  inflight.set(key, promise);
  return promise;
};

export async function getOrSet<T>(
  key: string,
  ttlSeconds: number,
  fetchFn: () => Promise<T>,
  namespace: CacheNamespace = "dashboard"
): Promise<{ value: T; cacheStatus: "HIT" | "MISS" | "SKIP" }> {
  const redisOn = isCacheNamespaceEnabled(namespace);
  const hotOn = isHotCacheEnabled();

  if (!redisOn && !hotOn) {
    const value = await fetchFn();
    logCacheEvent("SKIP", key, namespace);
    return { value, cacheStatus: "SKIP" };
  }

  const start = Date.now();
  const hot = readHot(key);

  if (hot?.fresh) {
    logCacheEvent("HIT", key, namespace, Date.now() - start);
    return { value: hot.value as T, cacheStatus: "HIT" };
  }

  if (hot && !hot.fresh) {
    void loadFresh(key, ttlSeconds, fetchFn, namespace).catch((err: any) => {
      logger.warn(`[appCache] refresh failed for ${key}: ${err?.message || err}`);
    });
    logCacheEvent("HIT", key, namespace, Date.now() - start);
    return { value: hot.value as T, cacheStatus: "HIT" };
  }

  if (redisOn) {
    try {
      const cached = await cacheLayer.get(key);
      if (cached) {
        rememberHot(key, cached, ttlSeconds);
        logCacheEvent("HIT", key, namespace, Date.now() - start);
        return { value: JSON.parse(cached) as T, cacheStatus: "HIT" };
      }
    } catch (err: any) {
      logCacheEvent("ERROR", key, namespace);
      logger.warn(`[appCache] get failed for ${key}: ${err?.message || err}`);
    }
  }

  const value = await loadFresh(key, ttlSeconds, fetchFn, namespace);
  logCacheEvent("MISS", key, namespace, Date.now() - start);
  return { value, cacheStatus: "MISS" };
}

export async function invalidatePattern(pattern: string): Promise<void> {
  bumpPrefix(pattern);
  clearHotKeys(pattern);
  try {
    await cacheLayer.delFromPattern(pattern);
  } catch (err: any) {
    logger.warn(`[appCache] invalidate failed for ${pattern}: ${err?.message || err}`);
  }
}

export async function invalidateCompany(
  companyId: number,
  namespaces: CacheNamespace[]
): Promise<void> {
  await Promise.all(
    namespaces.map(ns => invalidatePattern(`cc:${companyId}:${ns}:*`))
  );
}

export async function invalidateUser(userId: number): Promise<void> {
  await invalidatePattern(`cc:user:${userId}:*`);
}

export const appCache = {
  buildKey,
  buildUserKey,
  getOrSet,
  invalidatePattern,
  invalidateCompany,
  invalidateUser,
  isCacheEnabled,
  isHotCacheEnabled,
  isCacheNamespaceEnabled,
  CACHE_TTL
};
