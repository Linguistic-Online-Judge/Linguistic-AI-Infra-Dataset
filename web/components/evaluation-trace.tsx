const steps = [
  ["01", "登记语料"],
  ["02", "受控输入"],
  ["03", "固定模型"],
  ["04", "代码评分"],
] as const;

export function EvaluationTrace() {
  return (
    <section className="evaluation-trace" aria-labelledby="trace-title">
      <div className="evaluation-trace__heading">
        <p className="evaluation-trace__label">评测链路</p>
        <h2 id="trace-title">一次结果，四项固定依据</h2>
      </div>
      <ol className="evaluation-trace__steps">
        {steps.map(([number, label]) => (
          <li key={number}>
            <span className="evaluation-trace__number">{number}</span>
            <span>{label}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
