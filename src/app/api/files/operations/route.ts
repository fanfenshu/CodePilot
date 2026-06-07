import { NextRequest, NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';
import os from 'os';
import type { FileOperationRequest, FileOperationResponse, ErrorResponse } from '@/types';
import { isPathSafe } from '@/lib/files';

// Forbidden system directories
const FORBIDDEN_PATHS = [
  '/System',
  '/Library',
  '/bin',
  '/sbin',
  '/usr',
  '/etc',
  '/var',
  '/private',
  '/Applications',
  'C:\\Windows',
  'C:\\Program Files',
  'C:\\Program Files (x86)',
];

function isForbiddenPath(targetPath: string): boolean {
  const resolved = path.resolve(targetPath);
  return FORBIDDEN_PATHS.some(forbidden =>
    resolved.startsWith(forbidden) || resolved === forbidden
  );
}

export async function POST(request: NextRequest) {
  try {
    const body: FileOperationRequest = await request.json();
    const { action, path: targetPath, newPath, content, baseDir } = body;

    // Validate required fields
    if (!action || !targetPath) {
      return NextResponse.json<ErrorResponse>(
        { error: 'Missing required fields: action and path' },
        { status: 400 }
      );
    }

    const resolvedPath = path.resolve(targetPath);

    // Safety check: prevent system directory access
    if (isForbiddenPath(resolvedPath)) {
      return NextResponse.json<ErrorResponse>(
        { error: 'Access to system directories is forbidden' },
        { status: 403 }
      );
    }

    // If baseDir provided, ensure path stays within it
    if (baseDir && !isPathSafe(baseDir, resolvedPath)) {
      return NextResponse.json<ErrorResponse>(
        { error: 'Path escapes the allowed base directory' },
        { status: 403 }
      );
    }

    let response: FileOperationResponse;

    switch (action) {
      case 'create_file': {
        // Ensure parent directory exists
        const parentDir = path.dirname(resolvedPath);
        if (!fs.existsSync(parentDir)) {
          fs.mkdirSync(parentDir, { recursive: true });
        }

        // Create the file
        fs.writeFileSync(resolvedPath, content || '', 'utf-8');
        response = { success: true, path: resolvedPath };
        break;
      }

      case 'create_dir': {
        if (fs.existsSync(resolvedPath)) {
          return NextResponse.json<ErrorResponse>(
            { error: 'Directory already exists' },
            { status: 409 }
          );
        }
        fs.mkdirSync(resolvedPath, { recursive: true });
        response = { success: true, path: resolvedPath };
        break;
      }

      case 'delete': {
        if (!fs.existsSync(resolvedPath)) {
          return NextResponse.json<ErrorResponse>(
            { error: 'File or directory does not exist' },
            { status: 404 }
          );
        }

        const stat = fs.statSync(resolvedPath);
        if (stat.isDirectory()) {
          fs.rmSync(resolvedPath, { recursive: true, force: true });
        } else {
          fs.unlinkSync(resolvedPath);
        }
        response = { success: true, path: resolvedPath };
        break;
      }

      case 'rename': {
        if (!newPath) {
          return NextResponse.json<ErrorResponse>(
            { error: 'newPath is required for rename action' },
            { status: 400 }
          );
        }

        if (!fs.existsSync(resolvedPath)) {
          return NextResponse.json<ErrorResponse>(
            { error: 'Source file or directory does not exist' },
            { status: 404 }
          );
        }

        const resolvedNewPath = path.resolve(newPath);

        // Safety check for new path
        if (isForbiddenPath(resolvedNewPath)) {
          return NextResponse.json<ErrorResponse>(
            { error: 'Access to system directories is forbidden' },
            { status: 403 }
          );
        }

        if (baseDir && !isPathSafe(baseDir, resolvedNewPath)) {
          return NextResponse.json<ErrorResponse>(
            { error: 'New path escapes the allowed base directory' },
            { status: 403 }
          );
        }

        if (fs.existsSync(resolvedNewPath)) {
          return NextResponse.json<ErrorResponse>(
            { error: 'Target path already exists' },
            { status: 409 }
          );
        }

        fs.renameSync(resolvedPath, resolvedNewPath);
        response = { success: true, path: resolvedNewPath };
        break;
      }

      default:
        return NextResponse.json<ErrorResponse>(
          { error: `Unknown action: ${action}` },
          { status: 400 }
        );
    }

    return NextResponse.json<FileOperationResponse>(response);
  } catch (error) {
    console.error('File operation error:', error);
    return NextResponse.json<ErrorResponse>(
      { error: error instanceof Error ? error.message : 'Unknown error occurred' },
      { status: 500 }
    );
  }
}
