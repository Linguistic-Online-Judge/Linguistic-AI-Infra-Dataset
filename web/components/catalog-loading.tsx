export function CatalogLoading() {
  return (
    <main
      id="main-content"
      className="page-shell loading-state"
      aria-busy="true"
      aria-live="polite"
    >
      <h1>正在加载公开题目</h1>
      <div className="loading-state__rules" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
    </main>
  );
}
