import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: "Relay — QA workspace",
  description: "Review a plan. Run your tests. Follow the evidence.",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
