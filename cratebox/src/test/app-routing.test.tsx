import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, rootRouteId, RouterProvider } from "@tanstack/react-router";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routeTree } from "@/routeTree.gen";

// Render the page without the document shell and its external stylesheets.
vi.mock("@/routes/__root", async () => {
  const { createRootRouteWithContext, Outlet } = await import("@tanstack/react-router");
  return { Route: createRootRouteWithContext<{ queryClient: QueryClient }>()({ component: Outlet }) };
});

describe("App routing", () => {
  it("matches a page for / instead of falling back to not found", () => {
    const router = createRouter({ routeTree, context: { queryClient: new QueryClient() } });

    const matches = router.matchRoutes("/");

    expect(matches.at(-1)?.routeId).not.toBe(rootRouteId);
  });
});

describe("Rekordbox playlist routing", () => {
  const crate = { id: "local-crate", name: "Local Crate", trackCount: 0, duration: 0, created: "" };
  const playlists = [
    { id: "10", name: "Test Folder", is_folder: true, parent_id: null, track_count: 0, duration: 0 },
    { id: "11", name: "Test Playlist", is_folder: false, parent_id: "10", track_count: 2, duration: 360 },
  ];
  const tracks = [
    { id: "entry-a", rekordbox_id: "101", title: "First Track" },
    { id: "entry-b", rekordbox_id: "102", title: "Second Track" },
  ].map(track => ({
    ...track, artist: "Artist", album: "", duration: 180, rating: 0, bpm: 120, key: "8A",
    quality: "", url: "/api/collection/stream",
  }));
  const crateTrack = {
    ...tracks[1],
    id: "local-entry",
    playlist_count: 1,
    playlists: [{ id: "11", name: "Test Playlist" }],
  };
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("fetch", async (input: string) => {
      switch (new URL(input).pathname) {
        case "/api/playlists": return Response.json([crate]);
        case "/api/playlists/local-crate": return Response.json({ ...crate, tracks: [crateTrack] });
        case "/api/playlists/revision": return Response.json({ revision: "1" });
        case "/api/collection/status": return Response.json({ state: "ready", message: null });
        case "/api/rekordbox/playlists": return Response.json(playlists);
        case "/api/rekordbox/playlists/11/detail":
          return Response.json({ id: "11", name: "Test Playlist", revision: "1", editable: false, tracks });
        default: return new Response(null, { status: 404 });
      }
    });
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { value: vi.fn(), configurable: true });
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function renderRoute(url: string) {
    const history = createMemoryHistory({ initialEntries: [url] });
    const router = createRouter({ routeTree, history, context: { queryClient: new QueryClient() } });
    await act(async () => {
      await router.load();
      root.render(<RouterProvider router={router} />);
    });
    return router;
  }

  async function clickButton(label: string) {
    const button = Array.from(container.querySelectorAll("button")).find(
      (element) => element.getAttribute("aria-label") === label
        || element.textContent?.trim().startsWith(label),
    );
    expect(button).toBeDefined();
    await act(async () => button?.click());
  }

  it("links the selected sidebar playlist and clears the link in a folder or CrateBox", async () => {
    const router = await renderRoute("/?crate_playlist_id=local-crate");
    await clickButton("Rekordbox");
    expect(router.state.location.search).toEqual({});
    await clickButton("Test Folder");
    await clickButton("Test Playlist");
    expect(router.state.location.search).toEqual({ rekordbox_playlist_id: "11" });
    expect(container.querySelector("h1")).toHaveTextContent("Test Playlist");
    await clickButton("Back to parent folder");
    expect(router.state.location.search).toEqual({});
    expect(container.querySelector('[role="tab"][aria-selected="true"]')).toHaveTextContent("Rekordbox");
    await clickButton("CrateBox");
    expect(router.state.location.search).toEqual({ crate_playlist_id: "local-crate" });
  });

  it("opens a nested playlist from a playlist-only URL", async () => {
    const router = await renderRoute("/?rekordbox_playlist_id=11");
    expect(container.querySelector("h1")).toHaveTextContent("Test Playlist");
    expect(container.textContent).toContain("Second Track");
    expect(container.textContent).toContain("Rekordbox · Test Folder");

    const secondTrackRow = Array.from(container.querySelectorAll("tr")).find((row) =>
      row.textContent?.includes("Second Track"),
    );
    expect(secondTrackRow).toBeDefined();
    await act(async () => secondTrackRow?.click());
    expect(router.state.location.search).toEqual({
      rekordbox_playlist_id: "11",
      rekordbox_track_id: "102",
    });
  });

  it("keeps the artwork filter on when double-clicking a Rekordbox track", async () => {
    const router = await renderRoute("/?rekordbox_playlist_id=11");
    await clickButton("Show tracks without artwork");
    const secondTrackRow = Array.from(container.querySelectorAll("tr")).find((row) =>
      row.textContent?.includes("Second Track"),
    );
    expect(secondTrackRow).toBeDefined();

    await act(async () => {
      secondTrackRow?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      secondTrackRow?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      secondTrackRow?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    });

    expect(router.state.location.search).toEqual({
      rekordbox_playlist_id: "11",
      rekordbox_track_id: "102",
    });
    const artworkFilterButton = container.querySelector('button[aria-label="Show all tracks"]');
    expect(artworkFilterButton).not.toBeNull();
    expect(artworkFilterButton).toHaveAttribute("aria-pressed", "true");
  });

  it("opens a membership link at its track and returns to the crate with Back", async () => {
    const router = await renderRoute("/?crate_playlist_id=local-crate");
    const link = container.querySelector<HTMLAnchorElement>('a[title="Open Test Playlist at this track"]');
    expect(link).toBeDefined();
    await act(async () => link?.click());
    expect(router.state.location.search).toEqual({
      rekordbox_playlist_id: "11", rekordbox_track_id: "102",
    });
    expect(container.querySelector('tr[aria-selected="true"]')).toHaveTextContent("Second Track");

    await act(async () => router.history.back());
    expect(container.querySelector("h1")).toHaveTextContent("Local Crate");
    await act(async () => router.history.forward());
    expect(container.querySelector("h1")).toHaveTextContent("Test Playlist");
    expect(container.querySelector('tr[aria-selected="true"]')).toHaveTextContent("Second Track");

    await clickButton("Test Playlist");
    expect(router.state.location.search).toEqual({ rekordbox_playlist_id: "11" });
  });
});
