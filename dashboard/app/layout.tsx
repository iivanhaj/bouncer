import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "Bouncer dashboard",
  description: "Keys, usage and blocked requests for the Bouncer LLM gateway",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <span className="brand">Bouncer</span>
          <nav>
            <Link href="/">Keys</Link>
            <Link href="/usage">Usage</Link>
            <Link href="/blocked">Blocked</Link>
          </nav>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
