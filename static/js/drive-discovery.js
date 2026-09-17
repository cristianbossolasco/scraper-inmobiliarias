(function (root, factory) {
  "use strict";
  const utils = typeof module === "object" && module.exports ? require("./drive-utils.js") : root.RadarDriveUtils;
  const api = factory(utils);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.RadarDriveDiscovery = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (utils) {
  "use strict";
  const MAX_PROPERTIES = 3000;
  const CLOSE_M = 120;
  const fields = ["id", "latitude", "longitude", "price", "currency", "price_short", "type", "type_label", "group_id", "group_count", "group_suspicious", "group_price_short"];

  function accumulate(session, properties, position, knownIds = null, now = Date.now()) {
    if (!session || !utils.usablePosition(position, now)) return { added: 0, limited: false };
    session.encounters ||= {};
    session.details ||= {};
    let size = Object.keys(session.details).length;
    let added = 0, limited = false;
    for (const property of properties) {
      if (!Number.isInteger(property.id) || !Number.isFinite(property.latitude) || !Number.isFinite(property.longitude)) continue;
      const id = String(property.id);
      const distance = Math.round(utils.distanceMeters(position, property));
      const group = property.group_id || `${property.latitude.toFixed(6)},${property.longitude.toFixed(6)}`;
      const isNew = !session.details[id];
      if (isNew && size >= MAX_PROPERTIES) { limited = true; continue; }
      if (isNew) {
        session.details[id] = Object.fromEntries(fields.filter((key) => property[key] !== undefined).map((key) => [key, property[key]]));
        session.details[id].group_id = group;
        session.details[id].seen_before = knownIds === null ? null : knownIds.has(property.id);
        size += 1; added += 1;
      }
      const entry = session.encounters[group] ||= { firstSeenAt: now, minDistanceM: distance, propertyIds: [] };
      if (!entry.propertyIds.includes(property.id)) entry.propertyIds.push(property.id);
      entry.minDistanceM = Math.min(entry.minDistanceM, distance);
    }
    return { added, limited };
  }

  function properties(session) {
    if (!session) return [];
    const entries = new Map();
    for (const [group, encounter] of Object.entries(session.encounters || {})) {
      for (const id of encounter.propertyIds || []) entries.set(id, { group, encounter });
    }
    return [...new Set([...entries.keys(), ...(session.favoritePropertyIds || [])])].map((id) => {
      const detail = session.details?.[id] || { id };
      const entry = entries.get(id);
      const groupCount = Math.max(detail.group_count || 0, entry?.encounter.propertyIds.length || 1);
      return { ...detail, id, group_id: detail.group_id || entry?.group || `property-${id}`,
        group_count: groupCount, group_suspicious: Boolean(detail.group_suspicious || groupCount >= 5),
        firstSeenAt: entry?.encounter.firstSeenAt, minDistanceM: entry?.encounter.minDistanceM,
        passedClose: entry?.encounter.minDistanceM <= CLOSE_M,
        favorite: (session.favoritePropertyIds || []).includes(id) };
    }).sort((a, b) => (a.firstSeenAt || 0) - (b.firstSeenAt || 0) || a.id - b.id);
  }

  function filter(properties, mode, pendingIds = new Set()) {
    return properties.filter((property) => mode === "close" ? property.passedClose
      : mode === "favorites" ? property.favorite
      : mode === "pending" ? pendingIds.has(property.id)
      : mode === "new" ? property.seen_before === false : true);
  }

  return { accumulate, properties, filter, MAX_PROPERTIES, CLOSE_M };
});
