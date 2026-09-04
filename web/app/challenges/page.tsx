import type { Metadata } from "next";
import { Suspense } from "react";

import { CatalogLoading } from "@/components/catalog-loading";
import { ChallengeList } from "@/components/challenge-list";
import { EvaluationTrace } from "@/components/evaluation-trace";
import { getChallenges } from "@/lib/challenge-api";

export const metadata: Metadata = {
  title: "公开题目",
  description: "浏览已登记的语言学评测题目、指标和开放状态。",
};

export const dynamic = "force-dynamic";

async function ChallengeCatalog() {
  const challenges = await getChallenges();

  return (
    <main id="main-content" className="page-shell catalog-page">
      <header className="page-intro">
        <div>
          <h1>公开题目</h1>
        </div>
        <p className="page-intro__summary">
          选择题目，查看任务、评测指标、版本和提交状态。
        </p>
      </header>

      <EvaluationTrace />
      <ChallengeList challenges={challenges} />
    </main>
  );
}

export default function ChallengesPage() {
  return (
    <Suspense fallback={<CatalogLoading />}>
      <ChallengeCatalog />
    </Suspense>
  );
}
