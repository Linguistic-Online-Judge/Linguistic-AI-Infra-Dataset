"use client";

export default function ChallengesError({ retry }: { retry: () => void }) {
  return (
    <main id="main-content" className="page-shell message-page">
      <h1>无法加载公开题目</h1>
      <p>题目服务暂时不可用，请稍后重试。</p>
      <button className="primary-action" type="button" onClick={retry}>
        重新加载题目
      </button>
    </main>
  );
}
