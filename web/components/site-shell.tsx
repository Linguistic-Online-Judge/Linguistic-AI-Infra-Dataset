import Link from "next/link";

export function SiteHeader() {
  return (
    <header className="site-header">
      <div className="site-header__inner">
        <Link
          className="brand"
          href="/challenges"
          aria-label="语言学在线测评题目列表"
        >
          <span className="brand__mark" aria-hidden="true">
            言
          </span>
          <span className="brand__name">
            <strong>语言学在线测评</strong>
            <span>LINGUISTIC ONLINE JUDGE</span>
          </span>
        </Link>
        <span className="site-header__section">公开评测目录</span>
      </div>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="site-footer__inner">
        <p>分数由固定代码计算；相同题目版本使用相同评测规则。</p>
        <p className="site-footer__registry">LOJ / PUBLIC REGISTRY</p>
      </div>
    </footer>
  );
}
