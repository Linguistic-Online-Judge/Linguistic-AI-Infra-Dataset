"use strict";
const TEMPLATE_RULES = {
  "version": "teaching-rules-2",
  "segmentation": "输入 text 由树库词元拼接而成。请恢复词元边界，只在现有字符之间划分，不增加、删除、替换字符，也不补写空格。输出前核对：按顺序直接拼接 tokens 中的所有字符串，必须与输入 text 逐字符相同。标点也需保留。{note}只返回一个 JSON 对象，唯一字段 tokens 为非空字符串数组；不输出解释、代码围栏或额外字段。",
  "dependency": "按当前树库的通用依存关系规则标注输入 tokens。为每个输入 token_id 返回且只返回一条依存弧，不重新分词、不新增或遗漏编号。head_id 必须是0或输入中已有的编号，不能等于自身编号；只有句根使用head_id=0和deprel=root。标点也必须有对应记录。关系标签使用UD标签及当前树库子类型。输出前核对编号集合与输入完全相同，并按token_id顺序排列。只返回 JSON 对象，唯一字段 arcs 为数组，每项只含 token_id、head_id、deprel；前两项为整数，deprel为字符串。不附词形、词性、解释或代码围栏。"
};
