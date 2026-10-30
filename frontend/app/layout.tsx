import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TrustFlow Demo — Sandbox",
  description: "Self-verifying multi-agent workflows. Synthetic data only.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
