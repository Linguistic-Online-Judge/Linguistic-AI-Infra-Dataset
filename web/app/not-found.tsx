import Link from "next/link";

export default function NotFound() {
  return (
    <main id="main-content" className="page-shell message-page">
      <p className="message-page__code">错误 404</p>
      <h1>页面不存在</h1>
      <p>请检查地址，或返回公开题目列表。</p>
      <Link className="primary-action" href="/challenges">
        查看公开题目
      </Link>
    </main>
  );
}
