import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { cache } from "react";

import { ChallengeDetailView } from "@/components/challenge-detail";
import { getChallenge, isChallengeId } from "@/lib/challenge-api";

export const dynamic = "force-dynamic";

interface ChallengePageProps {
  readonly params: Promise<{ challengeId: string }>;
}

const loadChallenge = cache((challengeId: string) => getChallenge(challengeId));

export async function generateMetadata({
  params,
}: ChallengePageProps): Promise<Metadata> {
  const { challengeId } = await params;
  if (!isChallengeId(challengeId)) {
    return { title: "未找到题目", robots: { index: false } };
  }
  const challenge = await loadChallenge(challengeId);
  return {
    title: challenge?.title ?? "未找到题目",
    robots: challenge === null ? { index: false } : undefined,
  };
}

export default async function ChallengePage({ params }: ChallengePageProps) {
  const { challengeId } = await params;
  if (!isChallengeId(challengeId)) {
    notFound();
  }
  const challenge = await loadChallenge(challengeId);
  if (challenge === null) {
    notFound();
  }

  return <ChallengeDetailView challenge={challenge} />;
}
