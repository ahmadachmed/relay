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
    <html lang="en" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){var t='system';try{var s=localStorage.getItem('relay.ui.theme');if(s==='light'||s==='dark')t=s;}catch(e){}document.documentElement.dataset.theme=t==='system'?(window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):t;})();`,
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
