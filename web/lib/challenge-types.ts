export interface ChallengeSummary {
  readonly challenge_id: string;
  readonly title: string;
  readonly version: string;
  readonly language: string;
  readonly treebank: string;
  readonly task: string;
  readonly sample_count: number;
  readonly primary_metric: string;
  readonly security_level: string;
  readonly status: string;
  readonly submissions_open: boolean;
}

export interface ChallengeDetail extends ChallengeSummary {
  readonly secondary_metrics: readonly string[];
  readonly response_schema_version: string;
  readonly scorer_version: string | null;
  readonly aggregation_version: string | null;
  readonly dataset_sha256: string;
  readonly selection_sha256: string;
}
