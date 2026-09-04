import Link from "next/link";

import { AvailabilityBadge } from "@/components/challenge-status";
import {
  formatSampleCount,
  languageLabel,
  metricPresentation,
  publicationLabel,
  taskPresentation,
} from "@/lib/challenge-presenter";
import type { ChallengeSummary } from "@/lib/challenge-types";

interface ChallengeListProps {
  readonly challenges: readonly ChallengeSummary[];
}

export function ChallengeList({ challenges }: ChallengeListProps) {
  if (challenges.length === 0) {
    return (
      <section className="empty-state" aria-labelledby="empty-title">
        <h2 id="empty-title">暂无公开题目</h2>
        <p>当前没有可公开查看的题目，请稍后再查看。</p>
      </section>
    );
  }

  const sortedChallenges = [...challenges].sort((left, right) =>
    left.challenge_id.localeCompare(right.challenge_id, "en"),
  );

  return (
    <section className="catalog" aria-labelledby="catalog-title">
      <div className="catalog__heading">
        <div>
          <h2 id="catalog-title">题目索引</h2>
        </div>
        <p className="catalog__count">共 {challenges.length} 道题目</p>
      </div>

      <div className="catalog__columns" aria-hidden="true">
        <span>题目</span>
        <span>语言 / 树库</span>
        <span>任务</span>
        <span>主要指标</span>
        <span>提交 / 发布</span>
      </div>

      <ul className="catalog__records">
        {sortedChallenges.map((challenge) => {
          const task = taskPresentation(challenge.task);
          const metric = metricPresentation(challenge.primary_metric);
          return (
            <li key={challenge.challenge_id}>
              <article className="challenge-record">
                <div className="challenge-record__identity">
                  <p className="field-label">题目</p>
                  <h3>
                    <Link
                      href={`/challenges/${encodeURIComponent(challenge.challenge_id)}`}
                    >
                      <span>{challenge.title}</span>
                      <span className="challenge-record__arrow" aria-hidden="true">
                        →
                      </span>
                    </Link>
                  </h3>
                  <code>{challenge.challenge_id}</code>
                </div>

                <div className="challenge-record__field">
                  <p className="field-label">语言 / 树库</p>
                  <strong>{languageLabel(challenge.language)}</strong>
                  <span>{challenge.treebank}</span>
                </div>

                <div className="challenge-record__field">
                  <p className="field-label">任务</p>
                  <strong>{task.label}</strong>
                  <span>{formatSampleCount(challenge.sample_count)} 个样本</span>
                </div>

                <div className="challenge-record__field">
                  <p className="field-label">主要指标</p>
                  <strong>{metric.label}</strong>
                  <code>{challenge.primary_metric}</code>
                </div>

                <div className="challenge-record__state">
                  <p className="field-label">提交 / 发布</p>
                  <AvailabilityBadge open={challenge.submissions_open} />
                  <span>{publicationLabel(challenge.status)}</span>
                </div>
              </article>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
