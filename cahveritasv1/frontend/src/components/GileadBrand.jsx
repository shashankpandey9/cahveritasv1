import React from 'react'

/**
 * PLACEHOLDER Gilead wordmark, styled per Gilead Sciences identity
 * (Gilead Red #C8102E). Swap for the licensed SVG from Gilead Brand Central
 * before any internal or external showing — see COPILOT_GUIDE.md §Branding.
 */
export default function GileadBrand() {
  return (
    <svg width="118" height="34" viewBox="0 0 118 34" role="img"
      aria-label="Gilead (placeholder wordmark)">
      <rect x="0" y="0" width="118" height="34" rx="4" fill="#C8102E" />
      <text x="59" y="23" textAnchor="middle" fill="#FFFFFF"
        fontFamily="Inter, system-ui, sans-serif" fontWeight="700"
        fontSize="17" letterSpacing="2.5">GILEAD</text>
    </svg>
  )
}
