import type { Metadata } from "next";
import "./globals.css";
import LampShell from "./components/LampShell";

export const metadata: Metadata = {
  title: "Karakos Dashboard",
  description: "Agent system monitoring and control",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" data-hour="dusk" suppressHydrationWarning>
      <head>
        {/* Pre-paint hour stamp — prevents a flash of the wrong palette.
            Boundaries duplicate app/lib/hour.ts; keep them in sync. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function() {
                try {
                  // One-time migration: any stored light/dark pin predates
                  // Lamplight's time-of-day palette, so it silently pinned
                  // day/night forever and the scheme never changed with the
                  // clock. Hand control back to the clock once; re-pinning
                  // via ThemeToggle still works and is left alone after.
                  if (!localStorage.getItem('karakos-lamplight-v1')) {
                    var t0 = localStorage.getItem('karakos-theme');
                    if (t0 === 'light' || t0 === 'dark') {
                      localStorage.setItem('karakos-theme', 'system');
                    }
                    localStorage.setItem('karakos-lamplight-v1', '1');
                  }
                  var b = null;
                  var pin = localStorage.getItem('karakos-hour');
                  var B = ['dawn','morning','day','afternoon','golden','dusk','night','late'];
                  if (pin && B.indexOf(pin) !== -1) b = pin;
                  if (!b) {
                    var t = localStorage.getItem('karakos-theme');
                    if (t === 'light') b = 'day';
                    else if (t === 'dark') b = 'night';
                  }
                  if (!b) {
                    var d = new Date();
                    var m = d.getHours() * 60 + d.getMinutes();
                    b = (m >= 300 && m < 450) ? 'dawn'
                      : (m >= 450 && m < 690) ? 'morning'
                      : (m >= 690 && m < 930) ? 'day'
                      : (m >= 930 && m < 1080) ? 'afternoon'
                      : (m >= 1080 && m < 1155) ? 'golden'
                      : (m >= 1155 && m < 1290) ? 'dusk'
                      : (m >= 1290 || m < 90) ? 'night'
                      : 'late';
                  }
                  document.documentElement.setAttribute('data-hour', b);
                } catch(e) {}
              })();
            `,
          }}
        />
      </head>
      <body className="m-0 font-sans bg-gray-950 text-gray-100">
        <LampShell />
        <div className="flex h-screen">
          <nav className="w-52 p-4 border-r border-gray-800 flex-shrink-0">
            <h2 className="text-lg mb-6 text-white font-semibold">Karakos</h2>
            <ul className="list-none p-0 m-0 space-y-2">
              {[
                { href: "/", label: "Home" },
                { href: "/agents", label: "Agents" },
                { href: "/conversations", label: "Conversations" },
                { href: "/chat", label: "Chat" },
                { href: "/system", label: "System" },
                { href: "/settings", label: "Settings" },
              ].map(({ href, label }) => (
                <li key={href}>
                  <a
                    href={href}
                    className="text-gray-400 hover:text-gray-200 no-underline text-sm block transition-colors"
                  >
                    {label}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
          <main className="flex-1 p-6 overflow-auto h-screen">
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
