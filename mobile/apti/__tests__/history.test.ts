// jest-expo's native-module mocking doesn't cover these two modules cleanly enough for a
// module-level `new Directory(...)` / AsyncStorage import to survive at import time (the
// test below only exercises the pure `applyCap` function, but importing '../src/history'
// still runs its module-level code). Mock both before importing.
jest.mock('@react-native-async-storage/async-storage', () =>
  require('@react-native-async-storage/async-storage/jest/async-storage-mock'),
);
jest.mock('expo-file-system', () => ({
  Directory: class {
    exists = false;
    create() {}
  },
  File: class {
    exists = false;
    delete() {}
    copy() {
      return Promise.resolve();
    }
    get uri() {
      return 'file://mock';
    }
  },
  Paths: { document: 'file://mock-document/' },
}));

import { applyCap, type HistoryEntry } from '../src/history';

function mk(id: string, timestamp: number): HistoryEntry {
  return { id, source: 'detect', label: 'Person', confidence: 0.9, severity: 'low', timestamp, thumbnailUri: null };
}

describe('applyCap', () => {
  it('keeps everything under the cap', () => {
    const { kept, dropped } = applyCap([mk('a', 1), mk('b', 2)], 5);
    expect(kept).toHaveLength(2);
    expect(dropped).toHaveLength(0);
  });

  it('drops the oldest entries over the cap', () => {
    const entries = [mk('old', 1), mk('mid', 2), mk('new', 3)];
    const { kept, dropped } = applyCap(entries, 2);
    expect(kept.map((e) => e.id)).toEqual(['new', 'mid']);
    expect(dropped.map((e) => e.id)).toEqual(['old']);
  });

  it('keeps exactly the cap when equal to the count', () => {
    const { kept, dropped } = applyCap([mk('a', 1), mk('b', 2)], 2);
    expect(kept).toHaveLength(2);
    expect(dropped).toHaveLength(0);
  });
});
