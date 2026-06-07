"use client";

import { FileBrowserTree } from "./FileBrowserTree";
import { cn } from "@/lib/utils";

interface FileBrowserPanelProps {
  open: boolean;
  rootDir: string;
  className?: string;
}

export function FileBrowserPanel({
  open,
  rootDir,
  className,
}: FileBrowserPanelProps) {
  if (!open) return null;

  return (
    <div
      className={cn(
        "flex h-full w-60 flex-col border-r border-border/50 bg-background",
        className
      )}
    >
      <FileBrowserTree rootDirectory={rootDir} />
    </div>
  );
}
