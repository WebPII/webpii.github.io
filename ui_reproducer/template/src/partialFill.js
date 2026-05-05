// partialFill.js - Helpers for partial input values
// Simulates a user filling form fields and stopping partway
// Config specifies exact field to be partial and which fields are empty

import data from '@data'

const config = data.PARTIAL_FILL_CONFIG || { enabled: false }

// Build sets for O(1) lookup
const emptyFieldsSet = new Set(config.emptyFields || [])

/**
 * Get the fill status for a field
 * @param {string} key - Field key
 * @returns {'full'|'partial'|'empty'|null}
 */
function computeFillStatus(key) {
  if (!config.enabled) return null

  if (key === config.partialField) {
    return 'partial'
  } else if (emptyFieldsSet.has(key)) {
    return 'empty'
  } else {
    return 'full'
  }
}

/**
 * Check if a field is affected by partial fill
 * @param {string} key - Field key
 * @returns {boolean}
 */
export function isPartialField(key) {
  const status = computeFillStatus(key)
  return status === 'partial' || status === 'empty'
}

/**
 * Get the value for a field based on form fill progress
 * Use this for input/textarea elements.
 *
 * @param {string} key - Field key (e.g., 'PII_EMAIL')
 * @returns {string} - Full value, truncated value, or empty string
 */
export function getFieldValue(key) {
  const fullValue = data[key] || ''
  const status = computeFillStatus(key)

  if (!status || status === 'full') {
    return fullValue
  } else if (status === 'partial') {
    // Use exact char count computed by Python based on field length
    const charCount = config.stopCharCount ?? Math.ceil(fullValue.length / 2)
    return fullValue.slice(0, Math.max(1, charCount))
  } else {
    // empty
    return ''
  }
}

/**
 * Get the fill status for a field (for data attributes)
 * @param {string} key - Field key
 * @returns {'full'|'partial'|'empty'|null}
 */
export function getFieldFillStatus(key) {
  return computeFillStatus(key)
}

/**
 * Get the char count for a partial field
 * @param {string} key - Field key
 * @returns {number|null}
 */
export function getPartialCharCount(key) {
  const status = computeFillStatus(key)
  if (status !== 'partial') return null
  return config.stopCharCount ?? null
}

/**
 * Get the value for a select/dropdown field
 * Returns full value, or null (show default/placeholder) if field should be empty
 *
 * @param {string} key - Field key (e.g., 'PII_STATE')
 * @returns {string|null} - Value to select, or null for default state
 *
 * @example
 * <select data-pii="PII_STATE" {...getSelectProps('PII_STATE')}>
 *   <option value="">Select state...</option>
 *   {states.map(s => <option key={s} value={s}>{s}</option>)}
 * </select>
 */
export function getSelectValue(key) {
  const fullValue = data[key] || ''
  const status = computeFillStatus(key)

  if (!status || status === 'full') {
    return fullValue
  } else {
    // partial or empty - show default/placeholder
    return null
  }
}

/**
 * Get props for select/dropdown fields (like getPartialProps but for selects)
 * Returns value and data attributes for detection
 *
 * @param {string} key - Field key (e.g., 'PII_STATE')
 * @returns {object} - Props to spread onto select element
 */
export function getSelectProps(key) {
  const value = getSelectValue(key)
  const fillStatus = getFieldFillStatus(key)

  if (!fillStatus || fillStatus === 'full') {
    return { value: value || '' }
  }

  // Mark as partial/empty for detection
  return {
    value: value || '',
    'data-partial': 'true',
    'data-fill-status': fillStatus,
  }
}

/**
 * Get props object for input fields
 * Returns value and data attributes for partial fill detection
 *
 * @param {string} key - Field key
 * @returns {object} - Props to spread onto input element
 *
 * @example
 * <input data-pii="PII_FIRSTNAME" {...getPartialProps('PII_FIRSTNAME')} />
 * <input data-pii="PII_EMAIL" {...getPartialProps('PII_EMAIL')} />
 */
export function getPartialProps(key) {
  const value = getFieldValue(key)
  const fillStatus = getFieldFillStatus(key)

  if (!fillStatus || fillStatus === 'full') {
    return { value }
  }

  const props = {
    value,
    'data-partial': 'true',
    'data-fill-status': fillStatus,
  }

  if (fillStatus === 'partial') {
    props['data-partial-char-count'] = getPartialCharCount(key)
  }

  return props
}
