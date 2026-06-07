declare global {
  interface Window {
    electronAPI?: {
      versions: {
        electron: string;
        node: string;
        chrome: string;
      };
      showItemInFolder: (path: string) => Promise<void>;
    };
  }
}

export {};
