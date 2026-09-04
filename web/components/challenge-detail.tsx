import Link from "next/link";

import { AvailabilityBadge } from "@/components/challenge-status";
import {
  formatSampleCount,
  languageLabel,
  metricPresentation,
  publicationLabel,
  securityLabel,
  taskPresentation,
  versionLabel,
} from "@/lib/challenge-presenter";
import type { ChallengeDetail } from "@/lib/challenge-types";

interface ChallengeDetailViewProps {
  readonly challenge: ChallengeDetail;
}

export function ChallengeDetailView({ challenge }: ChallengeDetailViewProps) {
  const task = taskPresentation(challenge.task);
  const primaryMetric = metricPresentation(challenge.primary_metric);

  return (
    <main id="main-content" className="page-shell detail-page">
      <Link className="back-link" href="/challenges">
        <span aria-hidden="true">←</span> 返回题目列表
      </Link>

      <header className="detail-header">
        <div className="detail-header__identity">
          <code>{challenge.challenge_id}</code>
          <h1>{challenge.title}</h1>
        </div>

        <div className="detail-header__availability">
          <AvailabilityBadge open={challenge.submissions_open} prominent />
          <p>
            {challenge.submissions_open
              ? "当前服务已开放此题提交。提交功能仅对已登录用户开放。"
              : "当前服务尚未开放此题提交，题目信息仍可查看。"}
          </p>
        </div>
      </header>

      <section className="fact-strip" aria-label="题目概览">
        <dl>
          <div>
            <dt>语言</dt>
            <dd>{languageLabel(challenge.language)}</dd>
          </div>
          <div>
            <dt>树库</dt>
            <dd>{challenge.treebank}</dd>
          </div>
          <div>
            <dt>任务</dt>
            <dd>{task.label}</dd>
          </div>
          <div>
            <dt>评测样本</dt>
            <dd>{formatSampleCount(challenge.sample_count)}</dd>
          </div>
          <div>
            <dt>题目版本</dt>
            <dd>{challenge.version}</dd>
          </div>
        </dl>
      </section>

      <div className="detail-layout">
        <div className="detail-main">
          <section className="content-section" aria-labelledby="task-heading">
            <h2 id="task-heading">任务说明</h2>
            <p>{task.description}</p>
            <p>
              系统使用固定版本的评测样本和模型执行每次提交，最终分数由固定评分代码计算。
            </p>
          </section>

          <section className="content-section" aria-labelledby="metric-heading">
            <h2 id="metric-heading">评测指标</h2>
            <div className="primary-metric">
              <p>主要指标</p>
              <strong>{primaryMetric.label}</strong>
              <code>{challenge.primary_metric}</code>
              <span>{primaryMetric.description}</span>
            </div>

            {challenge.secondary_metrics.length > 0 ? (
              <div className="secondary-metrics">
                <h3>辅助指标</h3>
                <ul>
                  {challenge.secondary_metrics.map((metric) => (
                    <li key={metric}>
                      <span>{metricPresentation(metric).label}</span>
                      <code>{metric}</code>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="secondary-metrics__empty">这道题目未登记辅助指标。</p>
            )}
          </section>
        </div>

        <aside className="registry-panel" aria-labelledby="registry-heading">
          <h2 id="registry-heading">公开登记</h2>
          <dl>
            <div>
              <dt>发布状态</dt>
              <dd>{publicationLabel(challenge.status)}</dd>
            </div>
            <div>
              <dt>评测集可复现性</dt>
              <dd>{securityLabel(challenge.security_level)}</dd>
            </div>
            <div>
              <dt>回答格式</dt>
              <dd>
                <code>{challenge.response_schema_version}</code>
              </dd>
            </div>
          </dl>
          <p className="registry-panel__note">
            页面不直接展示评测样本、标准答案或内部模型配置；当前评测集可由公开数据和构建规则复现。
          </p>
        </aside>
      </div>

      <details className="technical-record">
        <summary>
          <span>
            版本与完整性
          </span>
          <span className="technical-record__action technical-record__action--closed">
            展开记录
          </span>
          <span className="technical-record__action technical-record__action--open">
            收起记录
          </span>
        </summary>
        <dl>
          <div>
            <dt>题目版本</dt>
            <dd>{challenge.version}</dd>
          </div>
          <div>
            <dt>回答格式版本</dt>
            <dd>{challenge.response_schema_version}</dd>
          </div>
          <div>
            <dt>评分器版本</dt>
            <dd>{versionLabel(challenge.scorer_version)}</dd>
          </div>
          <div>
            <dt>汇总版本</dt>
            <dd>{versionLabel(challenge.aggregation_version)}</dd>
          </div>
          <div className="technical-record__hash">
            <dt>数据集 SHA-256</dt>
            <dd>
              <code>{challenge.dataset_sha256}</code>
            </dd>
          </div>
          <div className="technical-record__hash">
            <dt>样本选择 SHA-256</dt>
            <dd>
              <code>{challenge.selection_sha256}</code>
            </dd>
          </div>
        </dl>
      </details>
    </main>
  );
}
