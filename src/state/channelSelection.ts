import { create } from "zustand";

/**
 * The app-wide "current channel" selection, shared by the title-bar
 * channel switcher and the Channels screen (Style Lab scoping).
 *
 * `null` means "no explicit pick yet" — consumers fall back to the
 * first channel in the list, the same rule the Channels screen used
 * when this selection was its local state.
 */
interface ChannelSelectionState {
  selectedChannelId: string | null;
  setSelectedChannelId: (id: string | null) => void;
}

export const useChannelSelectionStore = create<ChannelSelectionState>()(
  (set) => ({
    selectedChannelId: null,
    setSelectedChannelId: (selectedChannelId) => set({ selectedChannelId }),
  }),
);
