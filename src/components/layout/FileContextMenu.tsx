"use client";

import { ReactNode } from "react";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  File01Icon,
  Folder01Icon,
  PencilEdit01Icon,
  Copy01Icon,
  FileSearchIcon,
  Delete01Icon,
} from "@hugeicons/core-free-icons";
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
} from "@/components/ui/context-menu";

export interface FileContextMenuProps {
  children: ReactNode;
  isDirectory: boolean;
  path: string;
  onNewFile?: () => void;
  onNewFolder?: () => void;
  onRename?: () => void;
  onCopyPath?: () => void;
  onShowInFinder?: () => void;
  onDelete?: () => void;
}

export function FileContextMenu({
  children,
  isDirectory,
  onNewFile,
  onNewFolder,
  onRename,
  onCopyPath,
  onShowInFinder,
  onDelete,
}: FileContextMenuProps) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent className="w-48">
        {isDirectory && (
          <>
            <ContextMenuItem onClick={onNewFile}>
              <HugeiconsIcon icon={File01Icon} className="mr-2 size-4" />
              New File
            </ContextMenuItem>
            <ContextMenuItem onClick={onNewFolder}>
              <HugeiconsIcon icon={Folder01Icon} className="mr-2 size-4" />
              New Folder
            </ContextMenuItem>
            <ContextMenuSeparator />
          </>
        )}
        <ContextMenuItem onClick={onRename}>
          <HugeiconsIcon icon={PencilEdit01Icon} className="mr-2 size-4" />
          Rename
        </ContextMenuItem>
        <ContextMenuItem onClick={onCopyPath}>
          <HugeiconsIcon icon={Copy01Icon} className="mr-2 size-4" />
          Copy Path
        </ContextMenuItem>
        <ContextMenuItem onClick={onShowInFinder}>
          <HugeiconsIcon icon={FileSearchIcon} className="mr-2 size-4" />
          Reveal in Finder
        </ContextMenuItem>
        <ContextMenuSeparator />
        <ContextMenuItem variant="destructive" onClick={onDelete}>
          <HugeiconsIcon icon={Delete01Icon} className="mr-2 size-4" />
          Delete
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}
