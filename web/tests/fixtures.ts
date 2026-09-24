import type { ChallengeDetail, ChallengeSummary } from "@/lib/challenge-types";

export const alphaChallenge: ChallengeSummary = {
  challenge_id: "zh-alpha-upos-v1",
  title: "中文通用词性标注",
  version: "1.0.0",
  language: "Chinese",
  treebank: "Alpha",
  task: "upos",
  sample_count: 50,
  primary_metric: "micro_accuracy",
  security_level: "public_reproducible",
  status: "active",
  submissions_open: true,
};

export const zetaChallenge: ChallengeSummary = {
  challenge_id: "en-zeta-dependency-v1",
  title: "English dependency parsing",
  version: "1.0.0",
  language: "English",
  treebank: "Zeta",
  task: "dependency",
  sample_count: 1200,
  primary_metric: "las",
  security_level: "public_reproducible",
  status: "draft",
  submissions_open: false,
};

export const alphaDetail: ChallengeDetail = {
  ...alphaChallenge,
  secondary_metrics: [],
  response_schema_version: "upos-v1",
  scorer_version: "1.0",
  aggregation_version: "1.0",
  dataset_sha256: "a".repeat(64),
  selection_sha256: "b".repeat(64),
};
