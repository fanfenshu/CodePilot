"use client";

import { useCallback } from "react";
import type { FileOperationRequest, FileOperationResponse } from "@/types";

export interface UseFileOperationsReturn {
  createFile: (path: string, content?: string) => Promise<FileOperationResponse>;
  createDirectory: (path: string) => Promise<FileOperationResponse>;
  deleteItem: (path: string) => Promise<FileOperationResponse>;
  rename: (oldPath: string, newPath: string) => Promise<FileOperationResponse>;
  copyPath: (path: string) => Promise<void>;
  showInFinder: (path: string) => Promise<void>;
}

async function performOperation(request: FileOperationRequest): Promise<FileOperationResponse> {
  const res = await fetch("/api/files/operations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });

  const data = await res.json();

  if (!res.ok) {
    return { success: false, error: data.error || "Operation failed" };
  }

  return data;
}

export function useFileOperations(baseDir?: string): UseFileOperationsReturn {
  const createFile = useCallback(
    async (path: string, content?: string): Promise<FileOperationResponse> => {
      return performOperation({
        action: "create_file",
        path,
        content,
        baseDir,
      });
    },
    [baseDir]
  );

  const createDirectory = useCallback(
    async (path: string): Promise<FileOperationResponse> => {
      return performOperation({
        action: "create_dir",
        path,
        baseDir,
      });
    },
    [baseDir]
  );

  const deleteItem = useCallback(
    async (path: string): Promise<FileOperationResponse> => {
      return performOperation({
        action: "delete",
        path,
        baseDir,
      });
    },
    [baseDir]
  );

  const rename = useCallback(
    async (oldPath: string, newPath: string): Promise<FileOperationResponse> => {
      return performOperation({
        action: "rename",
        path: oldPath,
        newPath,
        baseDir,
      });
    },
    [baseDir]
  );

  const copyPath = useCallback(async (path: string): Promise<void> => {
    await navigator.clipboard.writeText(path);
  }, []);

  const showInFinder = useCallback(async (path: string): Promise<void> => {
    if (typeof window !== "undefined" && window.electronAPI?.showItemInFolder) {
      await window.electronAPI.showItemInFolder(path);
    } else {
      console.warn("showItemInFolder not available (not running in Electron)");
    }
  }, []);

  return {
    createFile,
    createDirectory,
    deleteItem,
    rename,
    copyPath,
    showInFinder,
  };
}
