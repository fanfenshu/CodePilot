"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  RefreshIcon,
  Search01Icon,
  SourceCodeIcon,
  CodeIcon,
  File01Icon,
} from "@hugeicons/core-free-icons";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { FileTreeNode } from "@/types";
import {
  FileTree as AIFileTree,
  FileTreeFolder,
  FileTreeFile,
} from "@/components/ai-elements/file-tree";
import { FileContextMenu } from "./FileContextMenu";
import { useFileOperations } from "@/hooks/useFileOperations";
import type { ReactNode } from "react";
import path from "path-browserify";

interface FileBrowserTreeProps {
  rootDirectory: string;
  onRefresh?: () => void;
}

function getFileIcon(extension?: string): ReactNode {
  switch (extension) {
    case "ts":
    case "tsx":
    case "js":
    case "jsx":
    case "py":
    case "rb":
    case "rs":
    case "go":
    case "java":
    case "c":
    case "cpp":
    case "h":
    case "hpp":
    case "cs":
    case "swift":
    case "kt":
    case "dart":
    case "lua":
    case "php":
    case "zig":
      return (
        <HugeiconsIcon
          icon={SourceCodeIcon}
          className="size-4 text-muted-foreground"
        />
      );
    case "json":
    case "yaml":
    case "yml":
    case "toml":
      return (
        <HugeiconsIcon
          icon={CodeIcon}
          className="size-4 text-muted-foreground"
        />
      );
    default:
      return (
        <HugeiconsIcon
          icon={File01Icon}
          className="size-4 text-muted-foreground"
        />
      );
  }
}

function containsMatch(node: FileTreeNode, query: string): boolean {
  const q = query.toLowerCase();
  if (node.name.toLowerCase().includes(q)) return true;
  if (node.children) {
    return node.children.some((child) => containsMatch(child, q));
  }
  return false;
}

function filterTree(nodes: FileTreeNode[], query: string): FileTreeNode[] {
  if (!query) return nodes;
  return nodes
    .filter((node) => containsMatch(node, query))
    .map((node) => ({
      ...node,
      children: node.children ? filterTree(node.children, query) : undefined,
    }));
}

interface RenameState {
  path: string;
  name: string;
  isDirectory: boolean;
}

interface RenderTreeNodesProps {
  nodes: FileTreeNode[];
  searchQuery: string;
  renameState: RenameState | null;
  onStartRename: (node: FileTreeNode) => void;
  onFinishRename: (newName: string) => void;
  onCancelRename: () => void;
  onNewFile: (parentPath: string) => void;
  onNewFolder: (parentPath: string) => void;
  onCopyPath: (nodePath: string) => void;
  onShowInFinder: (nodePath: string) => void;
  onDelete: (nodePath: string) => void;
}

function RenderTreeNodes({
  nodes,
  searchQuery,
  renameState,
  onStartRename,
  onFinishRename,
  onCancelRename,
  onNewFile,
  onNewFolder,
  onCopyPath,
  onShowInFinder,
  onDelete,
}: RenderTreeNodesProps) {
  const filtered = searchQuery ? filterTree(nodes, searchQuery) : nodes;
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (renameState && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [renameState]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      onFinishRename(inputRef.current?.value || "");
    } else if (e.key === "Escape") {
      e.preventDefault();
      onCancelRename();
    }
  };

  return (
    <>
      {filtered.map((node) => {
        const isRenaming = renameState?.path === node.path;

        if (node.type === "directory") {
          return (
            <FileContextMenu
              key={node.path}
              path={node.path}
              isDirectory={true}
              onNewFile={() => onNewFile(node.path)}
              onNewFolder={() => onNewFolder(node.path)}
              onRename={() => onStartRename(node)}
              onCopyPath={() => onCopyPath(node.path)}
              onShowInFinder={() => onShowInFinder(node.path)}
              onDelete={() => onDelete(node.path)}
            >
              <div>
                {isRenaming ? (
                  <div className="flex items-center px-2 py-1">
                    <Input
                      ref={inputRef}
                      defaultValue={renameState.name}
                      className="h-6 text-xs"
                      onKeyDown={handleKeyDown}
                      onBlur={() => onFinishRename(inputRef.current?.value || "")}
                    />
                  </div>
                ) : (
                  <FileTreeFolder path={node.path} name={node.name}>
                    {node.children && (
                      <RenderTreeNodes
                        nodes={node.children}
                        searchQuery={searchQuery}
                        renameState={renameState}
                        onStartRename={onStartRename}
                        onFinishRename={onFinishRename}
                        onCancelRename={onCancelRename}
                        onNewFile={onNewFile}
                        onNewFolder={onNewFolder}
                        onCopyPath={onCopyPath}
                        onShowInFinder={onShowInFinder}
                        onDelete={onDelete}
                      />
                    )}
                  </FileTreeFolder>
                )}
              </div>
            </FileContextMenu>
          );
        }

        return (
          <FileContextMenu
            key={node.path}
            path={node.path}
            isDirectory={false}
            onRename={() => onStartRename(node)}
            onCopyPath={() => onCopyPath(node.path)}
            onShowInFinder={() => onShowInFinder(node.path)}
            onDelete={() => onDelete(node.path)}
          >
            <div>
              {isRenaming ? (
                <div className="flex items-center px-2 py-1">
                  <Input
                    ref={inputRef}
                    defaultValue={renameState.name}
                    className="h-6 text-xs"
                    onKeyDown={handleKeyDown}
                    onBlur={() => onFinishRename(inputRef.current?.value || "")}
                  />
                </div>
              ) : (
                <FileTreeFile
                  path={node.path}
                  name={node.name}
                  icon={getFileIcon(node.extension)}
                />
              )}
            </div>
          </FileContextMenu>
        );
      })}
    </>
  );
}

export function FileBrowserTree({
  rootDirectory,
  onRefresh,
}: FileBrowserTreeProps) {
  const [tree, setTree] = useState<FileTreeNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [renameState, setRenameState] = useState<RenameState | null>(null);
  const [newItemState, setNewItemState] = useState<{
    parentPath: string;
    type: "file" | "folder";
  } | null>(null);
  const newItemInputRef = useRef<HTMLInputElement>(null);

  const { createFile, createDirectory, deleteItem, rename, copyPath, showInFinder } =
    useFileOperations(rootDirectory);

  const fetchTree = useCallback(async () => {
    if (!rootDirectory) {
      setTree([]);
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(
        `/api/files?dir=${encodeURIComponent(rootDirectory)}&depth=4`
      );
      if (res.ok) {
        const data = await res.json();
        setTree(data.tree || []);
      } else {
        setTree([]);
      }
    } catch {
      setTree([]);
    } finally {
      setLoading(false);
    }
  }, [rootDirectory]);

  useEffect(() => {
    fetchTree();
  }, [fetchTree]);

  const handleRefresh = useCallback(() => {
    fetchTree();
    onRefresh?.();
  }, [fetchTree, onRefresh]);

  const handleStartRename = useCallback((node: FileTreeNode) => {
    setRenameState({
      path: node.path,
      name: node.name,
      isDirectory: node.type === "directory",
    });
  }, []);

  const handleFinishRename = useCallback(
    async (newName: string) => {
      if (!renameState || !newName || newName === renameState.name) {
        setRenameState(null);
        return;
      }

      const parentDir = path.dirname(renameState.path);
      const newPath = path.join(parentDir, newName);

      const result = await rename(renameState.path, newPath);
      if (result.success) {
        handleRefresh();
      } else {
        console.error("Rename failed:", result.error);
      }
      setRenameState(null);
    },
    [renameState, rename, handleRefresh]
  );

  const handleCancelRename = useCallback(() => {
    setRenameState(null);
  }, []);

  const handleNewFile = useCallback((parentPath: string) => {
    setNewItemState({ parentPath, type: "file" });
  }, []);

  const handleNewFolder = useCallback((parentPath: string) => {
    setNewItemState({ parentPath, type: "folder" });
  }, []);

  const handleCreateNewItem = useCallback(
    async (name: string) => {
      if (!newItemState || !name) {
        setNewItemState(null);
        return;
      }

      const newPath = path.join(newItemState.parentPath, name);
      let result;

      if (newItemState.type === "file") {
        result = await createFile(newPath, "");
      } else {
        result = await createDirectory(newPath);
      }

      if (result.success) {
        handleRefresh();
      } else {
        console.error("Create failed:", result.error);
      }
      setNewItemState(null);
    },
    [newItemState, createFile, createDirectory, handleRefresh]
  );

  const handleCopyPath = useCallback(
    async (nodePath: string) => {
      await copyPath(nodePath);
    },
    [copyPath]
  );

  const handleShowInFinder = useCallback(
    async (nodePath: string) => {
      await showInFinder(nodePath);
    },
    [showInFinder]
  );

  const handleDelete = useCallback(
    async (nodePath: string) => {
      const confirmed = window.confirm(
        `Are you sure you want to delete "${path.basename(nodePath)}"?`
      );
      if (!confirmed) return;

      const result = await deleteItem(nodePath);
      if (result.success) {
        handleRefresh();
      } else {
        console.error("Delete failed:", result.error);
      }
    },
    [deleteItem, handleRefresh]
  );

  useEffect(() => {
    if (newItemState && newItemInputRef.current) {
      newItemInputRef.current.focus();
    }
  }, [newItemState]);

  const handleNewItemKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleCreateNewItem(newItemInputRef.current?.value || "");
    } else if (e.key === "Escape") {
      e.preventDefault();
      setNewItemState(null);
    }
  };

  // Build default expanded set from first-level directories
  const defaultExpanded = new Set(
    tree.filter((n) => n.type === "directory").map((n) => n.path)
  );

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 p-2 border-b border-border/30">
        <p
          className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground"
          title={rootDirectory}
        >
          {rootDirectory || "No directory selected"}
        </p>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={handleRefresh}
          disabled={loading}
          className="h-6 w-6 shrink-0"
        >
          <HugeiconsIcon
            icon={RefreshIcon}
            className={cn("h-3 w-3", loading && "animate-spin")}
          />
          <span className="sr-only">Refresh</span>
        </Button>
      </div>

      {/* Search */}
      <div className="relative p-2 border-b border-border/30">
        <HugeiconsIcon
          icon={Search01Icon}
          className="absolute left-4 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground"
        />
        <Input
          placeholder="Filter files..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="h-7 pl-7 text-xs"
        />
      </div>

      {/* New Item Input */}
      {newItemState && (
        <div className="p-2 border-b border-border/30 bg-muted/30">
          <Input
            ref={newItemInputRef}
            placeholder={
              newItemState.type === "file" ? "New file name..." : "New folder name..."
            }
            className="h-7 text-xs"
            onKeyDown={handleNewItemKeyDown}
            onBlur={() => setNewItemState(null)}
          />
        </div>
      )}

      {/* Tree */}
      <div className="flex-1 overflow-auto">
        {loading && tree.length === 0 ? (
          <div className="flex items-center justify-center py-8">
            <HugeiconsIcon
              icon={RefreshIcon}
              className="h-4 w-4 animate-spin text-muted-foreground"
            />
          </div>
        ) : tree.length === 0 ? (
          <p className="py-4 text-center text-xs text-muted-foreground">
            {rootDirectory
              ? "No files found"
              : "Select a project folder to view files"}
          </p>
        ) : (
          <AIFileTree
            defaultExpanded={defaultExpanded}
            className="border-0 rounded-none"
          >
            <RenderTreeNodes
              nodes={tree}
              searchQuery={searchQuery}
              renameState={renameState}
              onStartRename={handleStartRename}
              onFinishRename={handleFinishRename}
              onCancelRename={handleCancelRename}
              onNewFile={handleNewFile}
              onNewFolder={handleNewFolder}
              onCopyPath={handleCopyPath}
              onShowInFinder={handleShowInFinder}
              onDelete={handleDelete}
            />
          </AIFileTree>
        )}
      </div>
    </div>
  );
}
