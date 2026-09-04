import Link from "next/link";

export default function ChallengeNotFound() {
  return (
    <main id="main-content" className="page-shell message-page">
      <p className="message-page__code">错误 404</p>
      <h1>未找到该题目</h1>
      <p>请检查地址中的题目标识，或返回题目列表重新选择。</p>
      <Link className="primary-action" href="/challenges">
        返回题目列表
      </Link>
    </main>
  );
}
