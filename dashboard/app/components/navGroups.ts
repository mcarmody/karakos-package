/**
 * Shared nav structure for the More screen and the desktop shelf. One source
 * so NavClient, TabShelf and MoreClient can't drift apart.
 */

export interface NavItem {
  href: string;
  label: string;
  detail?: string;
}

export interface NavGroup {
  title: string;
  tone: "near" | "far";
  items: NavItem[];
}

/** The tabs that live on the phone shelf plus desktop's "daily" group. */
export const DAILY_ITEMS: NavItem[] = [
  { href: "/chat", label: "Chat" },
  { href: "/", label: "Ops Board" },
];

export const NAV_GROUPS: NavGroup[] = [
  {
    title: "agents",
    tone: "far",
    items: [
      { href: "/agents", label: "Agents" },
      { href: "/fleet", label: "Fleet" },
      { href: "/memory", label: "Memory" },
      { href: "/history", label: "History" },
      { href: "/costs", label: "Costs" },
    ],
  },
  {
    title: "system",
    tone: "far",
    items: [{ href: "/settings", label: "Settings" }],
  },
];

/** The icon rail (1024-1279) can't fit the full grouped list: the daily
 * items plus Fleet, with a "More" catch-all mirroring the phone shelf. */
export const ICON_RAIL_ITEMS: NavItem[] = [...DAILY_ITEMS, { href: "/fleet", label: "Fleet" }];

/** Flat list: every route the two nav surfaces know about, daily first. */
export const ALL_NAV_ITEMS: NavItem[] = [...DAILY_ITEMS, ...NAV_GROUPS.flatMap((g) => g.items)];
