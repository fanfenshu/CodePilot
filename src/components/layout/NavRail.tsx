"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";
import { HugeiconsIcon } from "@hugeicons/react";
import type { IconSvgElement } from "@hugeicons/react";
import {
  Message02Icon,
  PlusSignIcon,
  Folder01Icon,
  GridIcon,
  Settings02Icon,
  Moon02Icon,
  Sun02Icon,
} from "@hugeicons/core-free-icons";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

interface NavRailProps {
  chatListOpen: boolean;
  onToggleChatList: () => void;
  fileBrowserOpen: boolean;
  onToggleFileBrowser: () => void;
}

type NavItemType = "link" | "toggle-chat" | "toggle-files";

interface NavItem {
  id: string;
  label: string;
  icon: typeof Message02Icon;
  type: NavItemType;
  href?: string;
}

const navItems: NavItem[] = [
  { id: "chats", href: "/chat", label: "Chats", icon: Message02Icon, type: "toggle-chat" },
  { id: "files", label: "Files", icon: Folder01Icon, type: "toggle-files" },
  { id: "extensions", href: "/extensions", label: "Extensions", icon: GridIcon, type: "link" },
  { id: "settings", href: "/settings", label: "Settings", icon: Settings02Icon, type: "link" },
];

export function NavRail({
  chatListOpen,
  onToggleChatList,
  fileBrowserOpen,
  onToggleFileBrowser,
}: NavRailProps) {
  const pathname = usePathname();
  const router = useRouter();
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  const isChatRoute = pathname === "/chat" || pathname.startsWith("/chat/");

  useEffect(() => {
    setMounted(true);
  }, []);

  return (
    <aside className="flex w-14 shrink-0 flex-col items-center bg-sidebar pb-3 pt-10">
      {/* New Chat */}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            asChild
            variant="ghost"
            size="icon"
            className="mb-2 h-9 w-9"
          >
            <Link href="/chat">
              <HugeiconsIcon icon={PlusSignIcon} className="h-4 w-4" />
              <span className="sr-only">New Chat</span>
            </Link>
          </Button>
        </TooltipTrigger>
        <TooltipContent side="right">New Chat</TooltipContent>
      </Tooltip>

      {/* Divider */}
      <div className="mx-auto mb-2 h-px w-6 bg-border/50" />

      {/* Nav icons */}
      <nav className="flex flex-1 flex-col items-center gap-1">
        {navItems.map((item) => {
          let isActive = false;
          if (item.type === "toggle-chat") {
            isActive = chatListOpen || isChatRoute;
          } else if (item.type === "toggle-files") {
            isActive = fileBrowserOpen;
          } else if (item.href) {
            isActive =
              item.href === "/extensions"
                ? pathname.startsWith("/extensions")
                : pathname === item.href || pathname.startsWith(item.href + "?");
          }

          const renderButton = () => {
            if (item.type === "toggle-chat") {
              return (
                <Button
                  variant="ghost"
                  size="icon"
                  className={cn(
                    "h-9 w-9",
                    isActive && "bg-sidebar-accent text-sidebar-accent-foreground"
                  )}
                  onClick={() => {
                    if (!isChatRoute) {
                      router.push("/chat");
                      onToggleChatList();
                    } else {
                      onToggleChatList();
                    }
                  }}
                >
                  <HugeiconsIcon icon={item.icon} className="h-4 w-4" />
                  <span className="sr-only">{item.label}</span>
                </Button>
              );
            }

            if (item.type === "toggle-files") {
              return (
                <Button
                  variant="ghost"
                  size="icon"
                  className={cn(
                    "h-9 w-9",
                    isActive && "bg-sidebar-accent text-sidebar-accent-foreground"
                  )}
                  onClick={onToggleFileBrowser}
                >
                  <HugeiconsIcon icon={item.icon} className="h-4 w-4" />
                  <span className="sr-only">{item.label}</span>
                </Button>
              );
            }

            return (
              <Button
                asChild
                variant="ghost"
                size="icon"
                className={cn(
                  "h-9 w-9",
                  isActive && "bg-sidebar-accent text-sidebar-accent-foreground"
                )}
              >
                <Link href={item.href!}>
                  <HugeiconsIcon icon={item.icon} className="h-4 w-4" />
                  <span className="sr-only">{item.label}</span>
                </Link>
              </Button>
            );
          };

          return (
            <Tooltip key={item.id}>
              <TooltipTrigger asChild>{renderButton()}</TooltipTrigger>
              <TooltipContent side="right">{item.label}</TooltipContent>
            </Tooltip>
          );
        })}
      </nav>

      {/* Bottom: theme toggle */}
      <div className="mt-auto flex flex-col items-center gap-2">
        {mounted && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
                className="h-8 w-8"
              >
                {theme === "dark" ? (
                  <HugeiconsIcon icon={Sun02Icon} className="h-4 w-4" />
                ) : (
                  <HugeiconsIcon icon={Moon02Icon} className="h-4 w-4" />
                )}
                <span className="sr-only">Toggle theme</span>
              </Button>
            </TooltipTrigger>
            <TooltipContent side="right">
              {theme === "dark" ? "Light mode" : "Dark mode"}
            </TooltipContent>
          </Tooltip>
        )}
      </div>
    </aside>
  );
}
