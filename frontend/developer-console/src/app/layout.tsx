import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "AIRIVU CSense — Developer Console",
  description: "Platform operations console",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body style={{ margin: 0, fontFamily: "system-ui, sans-serif" }}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
