const taskPresentations: Record<string, { label: string; description: string }> = {
  segmentation: {
    label: "分词",
    description: "识别连续文本中的词语边界。",
  },
  upos: {
    label: "通用词性标注",
    description: "为每个词元预测 Universal Dependencies（UD）的通用词性标签。",
  },
  xpos: {
    label: "语言特定词性标注",
    description: "根据树库的标注体系，为每个词元预测语言特定词性。",
  },
  dependency: {
    label: "依存句法分析",
    description: "预测词元之间的句法依存关系及其关系标签。",
  },
  transliteration: {
    label: "转写",
    description: "将输入形式转换为题目指定的文字或罗马化表示。",
  },
};

const languageLabels: Record<string, string> = {
  Arabic: "阿拉伯语",
  Bulgarian: "保加利亚语",
  Chinese: "中文",
  Czech: "捷克语",
  Danish: "丹麦语",
  Dutch: "荷兰语",
  English: "英语",
  Finnish: "芬兰语",
  French: "法语",
  German: "德语",
  Hebrew: "希伯来语",
  Hungarian: "匈牙利语",
  Italian: "意大利语",
  Japanese: "日语",
  Korean: "韩语",
  Portuguese: "葡萄牙语",
  Russian: "俄语",
  Spanish: "西班牙语",
  Swedish: "瑞典语",
  Thai: "泰语",
};

const metricPresentations: Record<
  string,
  { label: string; description: string }
> = {
  micro_f1: {
    label: "微平均 F1",
    description: "综合衡量全部样本中预测结果的精确率与召回率。",
  },
  micro_precision: {
    label: "微平均精确率",
    description: "衡量全部预测项中正确结果所占的比例。",
  },
  micro_recall: {
    label: "微平均召回率",
    description: "衡量全部标准项中被正确识别的比例。",
  },
  micro_accuracy: {
    label: "微平均准确率",
    description: "按全部标注单元计算正确预测所占的比例。",
  },
  las: {
    label: "带标签依存准确率",
    description: "同时衡量依存中心词与依存关系标签是否正确。",
  },
  uas: {
    label: "无标签依存准确率",
    description: "衡量依存中心词是否正确，不计关系标签。",
  },
  token_accuracy: {
    label: "词元准确率",
    description: "按全部词元计算完全正确的转写比例。",
  },
  sentence_exact_match_rate: {
    label: "整句完全匹配率",
    description: "衡量整句转写与标准结果完全一致的比例。",
  },
};

export function taskPresentation(task: string): {
  label: string;
  description: string;
} {
  return (
    taskPresentations[task] ?? {
      label: task,
      description: "暂未提供该任务的中文说明。",
    }
  );
}

export function languageLabel(language: string): string {
  return languageLabels[language] ?? language;
}

export function metricPresentation(metric: string): {
  label: string;
  description: string;
} {
  return (
    metricPresentations[metric] ?? {
      label: metric,
      description: "暂未提供该指标的中文定义。",
    }
  );
}

export function publicationLabel(status: string): string {
  if (status === "active") return "已发布";
  if (status === "draft") return "草稿";
  return status;
}

export function securityLabel(securityLevel: string): string {
  if (securityLevel === "public_reproducible") return "可由公开资料复现";
  return securityLevel;
}

export function formatSampleCount(sampleCount: number): string {
  return new Intl.NumberFormat("zh-CN").format(sampleCount);
}

export function versionLabel(version: string | null): string {
  return version ?? "未登记";
}
