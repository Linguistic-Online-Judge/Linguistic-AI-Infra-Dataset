import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { SiteFooter, SiteHeader } from "@/components/site-shell";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "语言学在线测评",
    template: "%s | 语言学在线测评",
  },
  description: "使用固定数据版本与确定性评分程序评测语言任务。",
};

export const viewport: Viewport = {
  colorScheme: "light",
  themeColor: "#f0f3f1",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <a className="skip-link" href="#main-content">
          跳到主要内容
        </a>
        <SiteHeader />
        {children}
        <SiteFooter />
      </body>
    </html>
  );
}
