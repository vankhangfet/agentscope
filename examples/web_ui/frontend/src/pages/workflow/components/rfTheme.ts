import type * as React from 'react';

/**
 * House-theme overrides for React Flow's CSS variables, so the edges,
 * controls, handles and dot grid follow the light/dark tokens instead
 * of the library's fixed greys. Shared by the builder canvas and the
 * read-only run graph (a plain module — not a component file — so
 * fast refresh keeps working around it).
 */
export const THEME_VARS = {
	'--xy-edge-stroke': 'var(--muted-foreground)',
	'--xy-edge-stroke-selected': 'var(--primary)',
	'--xy-edge-label-color': 'var(--foreground)',
	'--xy-edge-label-background-color': 'var(--card)',
	'--xy-controls-button-background-color': 'var(--card)',
	'--xy-controls-button-background-color-hover': 'var(--accent)',
	'--xy-controls-button-border-color': 'var(--border)',
	'--xy-controls-button-color': 'var(--foreground)',
	'--xy-background-pattern-color': 'var(--border)',
	'--xy-handle-background-color': 'var(--primary)',
	'--xy-handle-border-color': 'var(--card)',
} as React.CSSProperties;
