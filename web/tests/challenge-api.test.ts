import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ChallengeCatalogError,
  getChallenge,
  getChallenges,
  parseChallengeDetail,
} from "@/lib/challenge-api";
import { alphaChallenge, alphaDetail } from "@/tests/fixtures";

type Fetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("challenge catalog API", () => {
  it("requests the configured list endpoint and copies allowlisted fields", async () => {
    vi.stubEnv("LINGUISTIC_OJ_API_URL", "https://judge.example/api");
    const fetcher = vi.fn<Fetcher>(async () =>
      Response.json([{ ...alphaChallenge, private_manifest_path: "private.json" }]),
    );

    const challenges = await getChallenges(fetcher);

    expect(fetcher).toHaveBeenCalledWith(
      "https://judge.example/api/v1/challenges",
      expect.objectContaining({
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: expect.any(AbortSignal),
      }),
    );
    expect(challenges).toEqual([alphaChallenge]);
    expect(challenges[0]).not.toHaveProperty("private_manifest_path");
  });

  it("returns null when a challenge does not exist", async () => {
    const fetcher = vi.fn<Fetcher>(async () => new Response(null, { status: 404 }));

    await expect(getChallenge("missing-challenge", fetcher)).resolves.toBeNull();
    expect(fetcher.mock.calls[0][0].toString()).toContain("missing-challenge");
  });

  it("rejects a detail response for a different challenge", async () => {
    const fetcher = vi.fn<Fetcher>(async () => Response.json(alphaDetail));

    await expect(getChallenge("other-challenge", fetcher)).rejects.toThrow(
      "does not match the requested ID",
    );
  });

  it("reports service failures without exposing response content", async () => {
    const fetcher = vi.fn<Fetcher>(async () =>
      Response.json({ detail: "internal route" }, { status: 503 }),
    );

    await expect(getChallenges(fetcher)).rejects.toMatchObject({
      name: "ChallengeCatalogError",
      message: "Challenge catalog request failed.",
      status: 503,
    });
  });

  it("rejects malformed public metadata", () => {
    expect(() =>
      parseChallengeDetail({ ...alphaDetail, sample_count: "fifty" }),
    ).toThrow(ChallengeCatalogError);
    expect(() => parseChallengeDetail({ ...alphaDetail, sample_count: 0 })).toThrow(
      "must be a positive integer",
    );
  });

  it("rejects unsafe challenge IDs before making a request", async () => {
    const fetcher = vi.fn<Fetcher>();

    await expect(getChallenge("..", fetcher)).rejects.toThrow(
      "Challenge ID is invalid",
    );
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects oversized catalog responses", async () => {
    const fetcher = vi.fn<Fetcher>(async () =>
      new Response("[]", {
        headers: { "Content-Length": String(256 * 1024 + 1) },
      }),
    );

    await expect(getChallenges(fetcher)).rejects.toThrow(
      "Challenge API response is too large",
    );
  });

  it("stops reading a chunked catalog after the byte limit", async () => {
    const oversizedStream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(256 * 1024 + 1));
      },
      cancel() {
        throw new Error("upstream cancellation failed");
      },
    });
    const fetcher = vi.fn<Fetcher>(async () => new Response(oversizedStream));

    await expect(getChallenges(fetcher)).rejects.toThrow(
      "Challenge API response is too large",
    );
  });

  it("fails closed when production API configuration is missing", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("LINGUISTIC_OJ_API_URL", "");
    const fetcher = vi.fn<Fetcher>();

    await expect(getChallenges(fetcher)).rejects.toThrow(
      "LINGUISTIC_OJ_API_URL is required in production",
    );
    expect(fetcher).not.toHaveBeenCalled();
  });
});
