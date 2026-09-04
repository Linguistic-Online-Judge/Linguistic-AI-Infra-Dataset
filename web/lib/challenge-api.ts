import "server-only";

import type { ChallengeDetail, ChallengeSummary } from "@/lib/challenge-types";

type CatalogFetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

const REQUEST_TIMEOUT_MS = 5_000;
const LIST_RESPONSE_BYTES = 256 * 1024;
const DETAIL_RESPONSE_BYTES = 64 * 1024;
const CHALLENGE_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)+$/;

export class ChallengeCatalogError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ChallengeCatalogError";
  }
}

function catalogUrl(path: string): string {
  const configuredBase = process.env.LINGUISTIC_OJ_API_URL;
  if (!configuredBase && process.env.NODE_ENV === "production") {
    throw new ChallengeCatalogError(
      "LINGUISTIC_OJ_API_URL is required in production.",
    );
  }
  const apiBase = configuredBase ?? "http://127.0.0.1:8000";
  const base = apiBase.endsWith("/") ? apiBase : `${apiBase}/`;

  try {
    const parsedBase = new URL(base);
    if (
      !["http:", "https:"].includes(parsedBase.protocol) ||
      parsedBase.username ||
      parsedBase.password ||
      parsedBase.search ||
      parsedBase.hash
    ) {
      throw new TypeError("Unsupported challenge API URL.");
    }
    return new URL(path.replace(/^\//, ""), parsedBase).toString();
  } catch {
    throw new ChallengeCatalogError("Challenge API URL is invalid.");
  }
}

export function isChallengeId(value: string): boolean {
  return CHALLENGE_ID_PATTERN.test(value);
}

function validatedChallengeId(value: unknown): string {
  if (
    typeof value !== "string" ||
    !isChallengeId(value)
  ) {
    throw new ChallengeCatalogError("Challenge ID is invalid.");
  }
  return value;
}

function record(value: unknown, context: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ChallengeCatalogError(`${context} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function stringField(source: Record<string, unknown>, name: string): string {
  const value = source[name];
  if (typeof value !== "string") {
    throw new ChallengeCatalogError(`Challenge field ${name} must be a string.`);
  }
  return value;
}

function nullableStringField(
  source: Record<string, unknown>,
  name: string,
): string | null {
  const value = source[name];
  if (value !== null && typeof value !== "string") {
    throw new ChallengeCatalogError(
      `Challenge field ${name} must be a string or null.`,
    );
  }
  return value;
}

function integerField(source: Record<string, unknown>, name: string): number {
  const value = source[name];
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value <= 0
  ) {
    throw new ChallengeCatalogError(
      `Challenge field ${name} must be a positive integer.`,
    );
  }
  return value;
}

function sha256Field(source: Record<string, unknown>, name: string): string {
  const value = stringField(source, name);
  if (!/^[a-f0-9]{64}$/.test(value)) {
    throw new ChallengeCatalogError(
      `Challenge field ${name} must be a lowercase SHA-256 value.`,
    );
  }
  return value;
}

async function discardResponseBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // The original HTTP or size error is more useful than a cleanup failure.
  }
}

async function cancelReader(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): Promise<void> {
  try {
    await reader.cancel();
  } catch {
    // Preserve the response-size error when upstream cleanup also fails.
  }
}

function booleanField(source: Record<string, unknown>, name: string): boolean {
  const value = source[name];
  if (typeof value !== "boolean") {
    throw new ChallengeCatalogError(`Challenge field ${name} must be a boolean.`);
  }
  return value;
}

function stringListField(
  source: Record<string, unknown>,
  name: string,
): readonly string[] {
  const value = source[name];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new ChallengeCatalogError(
      `Challenge field ${name} must be a string array.`,
    );
  }
  return [...value];
}

export function parseChallengeSummary(value: unknown): ChallengeSummary {
  const source = record(value, "Challenge summary");
  return {
    challenge_id: validatedChallengeId(source.challenge_id),
    title: stringField(source, "title"),
    version: stringField(source, "version"),
    language: stringField(source, "language"),
    treebank: stringField(source, "treebank"),
    task: stringField(source, "task"),
    sample_count: integerField(source, "sample_count"),
    primary_metric: stringField(source, "primary_metric"),
    security_level: stringField(source, "security_level"),
    status: stringField(source, "status"),
    submissions_open: booleanField(source, "submissions_open"),
  };
}

export function parseChallengeDetail(value: unknown): ChallengeDetail {
  const source = record(value, "Challenge detail");
  return {
    ...parseChallengeSummary(source),
    secondary_metrics: stringListField(source, "secondary_metrics"),
    response_schema_version: stringField(source, "response_schema_version"),
    scorer_version: nullableStringField(source, "scorer_version"),
    aggregation_version: nullableStringField(source, "aggregation_version"),
    dataset_sha256: sha256Field(source, "dataset_sha256"),
    selection_sha256: sha256Field(source, "selection_sha256"),
  };
}

async function responseJson(
  response: Response,
  maxBytes: number,
): Promise<unknown> {
  const declaredLength = Number(response.headers.get("Content-Length"));
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    await discardResponseBody(response);
    throw new ChallengeCatalogError(
      "Challenge API response is too large.",
      response.status,
    );
  }

  try {
    if (response.body === null) {
      throw new SyntaxError("Response body is empty.");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let totalBytes = 0;
    let body = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      totalBytes += value.byteLength;
      if (totalBytes > maxBytes) {
        await cancelReader(reader);
        throw new ChallengeCatalogError(
          "Challenge API response is too large.",
          response.status,
        );
      }
      body += decoder.decode(value, { stream: true });
    }
    body += decoder.decode();
    return JSON.parse(body) as unknown;
  } catch (error) {
    if (error instanceof ChallengeCatalogError) {
      throw error;
    }
    throw new ChallengeCatalogError(
      "Challenge API returned invalid JSON.",
      response.status,
    );
  }
}

export async function getChallenges(
  fetcher: CatalogFetcher = fetch,
): Promise<readonly ChallengeSummary[]> {
  const response = await fetcher(catalogUrl("v1/challenges"), {
    cache: "no-store",
    headers: { Accept: "application/json" },
    redirect: "error",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) {
    await discardResponseBody(response);
    throw new ChallengeCatalogError(
      "Challenge catalog request failed.",
      response.status,
    );
  }

  const payload = await responseJson(response, LIST_RESPONSE_BYTES);
  if (!Array.isArray(payload)) {
    throw new ChallengeCatalogError("Challenge catalog must be an array.");
  }
  return payload.map(parseChallengeSummary);
}

export async function getChallenge(
  challengeId: string,
  fetcher: CatalogFetcher = fetch,
): Promise<ChallengeDetail | null> {
  validatedChallengeId(challengeId);
  const response = await fetcher(
    catalogUrl(`v1/challenges/${encodeURIComponent(challengeId)}`),
    {
      cache: "no-store",
      headers: { Accept: "application/json" },
      redirect: "error",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    },
  );
  if (response.status === 404) {
    await discardResponseBody(response);
    return null;
  }
  if (!response.ok) {
    await discardResponseBody(response);
    throw new ChallengeCatalogError(
      "Challenge detail request failed.",
      response.status,
    );
  }

  const challenge = parseChallengeDetail(
    await responseJson(response, DETAIL_RESPONSE_BYTES),
  );
  if (challenge.challenge_id !== challengeId) {
    throw new ChallengeCatalogError(
      "Challenge detail does not match the requested ID.",
    );
  }
  return challenge;
}
