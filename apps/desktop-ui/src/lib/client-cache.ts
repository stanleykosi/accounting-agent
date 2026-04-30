/*
Purpose: Provide one canonical browser-side cache for same-origin JSON workspace reads.
Scope: Fast snapshot hydration, request deduplication, short-lived session persistence, and broad invalidation after mutations.
Dependencies: Browser memory/sessionStorage only; server callers fall back to direct network reads.
*/

type CacheEntry<TValue> = Readonly<{
  expiresAt: number;
  value: TValue;
}>;

const DEFAULT_CACHE_TTL_MS = 30_000;
const STORAGE_SCHEMA_VERSION = 2;
const STORAGE_KEY_PREFIX = `accounting-ai-agent:json-cache:v${STORAGE_SCHEMA_VERSION}:`;

const memoryCache = new Map<string, CacheEntry<unknown>>();
const inFlightCache = new Map<string, Promise<unknown>>();

/**
 * Purpose: Read a fresh browser snapshot for one cache key without performing network I/O.
 * Inputs: The stable cache key used by the corresponding loader.
 * Outputs: The cached value when it is still fresh, otherwise null.
 * Behavior: Reads memory only so the first browser render matches the server render after hard refresh.
 */
export function readClientCacheSnapshot<TValue>(cacheKey: string): TValue | null {
  const memoryEntry = memoryCache.get(cacheKey);
  if (isFreshEntry(memoryEntry)) {
    return memoryEntry.value as TValue;
  }

  if (memoryEntry !== undefined) {
    memoryCache.delete(cacheKey);
  }

  return null;
}

/**
 * Purpose: Resolve one JSON resource through a short-lived browser cache before hitting the network.
 * Inputs: Cache key, async loader, and optional TTL override.
 * Outputs: The fresh cached value or the loader result when the cache is cold.
 * Behavior: Deduplicates concurrent requests and persists successful values into sessionStorage for refresh resilience.
 */
export async function loadClientCachedValue<TValue>(
  cacheKey: string,
  loader: () => Promise<TValue>,
  ttlMs = DEFAULT_CACHE_TTL_MS,
): Promise<TValue> {
  const snapshot = readPersistentClientCacheSnapshot<TValue>(cacheKey);
  if (snapshot !== null) {
    return snapshot;
  }

  const inFlightValue = inFlightCache.get(cacheKey);
  if (inFlightValue !== undefined) {
    return inFlightValue as Promise<TValue>;
  }

  const nextRequest = loader()
    .then((value) => {
      writeClientCacheValue(cacheKey, value, ttlMs);
      return value;
    })
    .finally(() => {
      inFlightCache.delete(cacheKey);
    });

  inFlightCache.set(cacheKey, nextRequest as Promise<unknown>);
  return nextRequest;
}

/**
 * Purpose: Persist one fresh cache value after a successful network read.
 * Inputs: Cache key, JSON-serializable value, and optional TTL override.
 * Outputs: None.
 * Behavior: Writes to memory first, then mirrors into sessionStorage when available.
 */
export function writeClientCacheValue<TValue>(
  cacheKey: string,
  value: TValue,
  ttlMs = DEFAULT_CACHE_TTL_MS,
): void {
  const entry: CacheEntry<TValue> = {
    expiresAt: Date.now() + ttlMs,
    value,
  };

  memoryCache.set(cacheKey, entry);

  if (!canUseBrowserCache()) {
    return;
  }

  try {
    window.sessionStorage.setItem(storageKey(cacheKey), JSON.stringify(entry));
  } catch {
    // Ignore storage quota and serialization failures; memory cache remains authoritative.
  }
}

/**
 * Purpose: Remove cached entries whose keys begin with any provided prefix.
 * Inputs: One or more cache-key prefixes.
 * Outputs: None.
 * Behavior: Clears memory and sessionStorage snapshots so subsequent reads refetch the current state.
 */
export function invalidateClientCacheByPrefix(prefixes: readonly string[]): void {
  if (prefixes.length === 0) {
    return;
  }

  const uniquePrefixes = [...new Set(prefixes)];

  for (const cacheKey of [...memoryCache.keys()]) {
    if (uniquePrefixes.some((prefix) => cacheKey.startsWith(prefix))) {
      memoryCache.delete(cacheKey);
      inFlightCache.delete(cacheKey);
    }
  }

  if (!canUseBrowserCache()) {
    return;
  }

  try {
    for (let index = window.sessionStorage.length - 1; index >= 0; index -= 1) {
      const currentStorageKey = window.sessionStorage.key(index);
      if (currentStorageKey === null || !currentStorageKey.startsWith(STORAGE_KEY_PREFIX)) {
        continue;
      }

      const cacheKey = currentStorageKey.slice(STORAGE_KEY_PREFIX.length);
      if (uniquePrefixes.some((prefix) => cacheKey.startsWith(prefix))) {
        window.sessionStorage.removeItem(currentStorageKey);
      }
    }
  } catch {
    // Ignore storage access failures; memory invalidation is still safe.
  }
}

/**
 * Purpose: Build one canonical invalidation set for an entity-scoped API mutation.
 * Inputs: The mutated same-origin API path.
 * Outputs: The cache-key prefixes that must be cleared to avoid stale entity and close-run workspace data.
 * Behavior: Invalidates broadly within the affected entity because there is no need to preserve historical local cache branches.
 */
export function buildEntityCacheInvalidationPrefixes(path: string): readonly string[] {
  const normalizedPath = normalizeCachePath(path);
  const pathname = normalizedPath.split("?")[0] ?? normalizedPath;

  const prefixes = new Set<string>(["/api/dashboard/bootstrap", "/api/entities"]);
  if (!pathname.startsWith("/api/entities/")) {
    prefixes.add(pathname);
    return [...prefixes];
  }

  const pathSegments = pathname.split("/").filter(Boolean);
  const entityId = pathSegments[2];
  if (typeof entityId !== "string" || entityId.length === 0) {
    prefixes.add(pathname);
    return [...prefixes];
  }

  const entityRoot = `/api/entities/${entityId}`;
  prefixes.add(entityRoot);
  prefixes.add(`${entityRoot}/close-runs`);
  prefixes.add(`${entityRoot}/reports`);

  if (pathSegments[3] === "close-runs" && typeof pathSegments[4] === "string") {
    prefixes.add(`${entityRoot}/close-runs/${pathSegments[4]}`);
  }

  return [...prefixes];
}

function canUseBrowserCache(): boolean {
  return typeof window !== "undefined" && typeof window.sessionStorage !== "undefined";
}

function isFreshEntry(entry: CacheEntry<unknown> | null | undefined): entry is CacheEntry<unknown> {
  return entry !== null && entry !== undefined && entry.expiresAt > Date.now();
}

function readPersistentClientCacheSnapshot<TValue>(cacheKey: string): TValue | null {
  const memorySnapshot = readClientCacheSnapshot<TValue>(cacheKey);
  if (memorySnapshot !== null) {
    return memorySnapshot;
  }

  const storageEntry = readStorageEntry(cacheKey);
  if (!isFreshEntry(storageEntry)) {
    clearStorageEntry(cacheKey);
    return null;
  }

  memoryCache.set(cacheKey, storageEntry);
  return storageEntry.value as TValue;
}

function readStorageEntry(cacheKey: string): CacheEntry<unknown> | null {
  if (!canUseBrowserCache()) {
    return null;
  }

  try {
    const rawValue = window.sessionStorage.getItem(storageKey(cacheKey));
    if (rawValue === null) {
      return null;
    }

    const parsedValue = JSON.parse(rawValue) as Partial<CacheEntry<unknown>>;
    if (typeof parsedValue.expiresAt !== "number" || !("value" in parsedValue)) {
      clearStorageEntry(cacheKey);
      return null;
    }

    return {
      expiresAt: parsedValue.expiresAt,
      value: parsedValue.value,
    };
  } catch {
    clearStorageEntry(cacheKey);
    return null;
  }
}

function clearStorageEntry(cacheKey: string): void {
  if (!canUseBrowserCache()) {
    return;
  }

  try {
    window.sessionStorage.removeItem(storageKey(cacheKey));
  } catch {
    // Ignore storage cleanup failures.
  }
}

function storageKey(cacheKey: string): string {
  return `${STORAGE_KEY_PREFIX}${cacheKey}`;
}

function normalizeCachePath(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) {
    try {
      const parsedUrl = new URL(path);
      return `${parsedUrl.pathname}${parsedUrl.search}`;
    } catch {
      return path;
    }
  }

  return path;
}
