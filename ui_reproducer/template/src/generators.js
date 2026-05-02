/**
 * Runtime generators for IDs, card numbers, tracking numbers, etc.
 *
 * These are generated at runtime using a seed from data.json, allowing
 * patterns to vary per website while keeping data consistent per variant.
 *
 * Usage:
 *   import { createGenerators } from '@generators'
 *   import data from '@data'
 *
 *   const gen = createGenerators(data.SEED)
 *
 *   // Generate IDs with patterns - pattern is REQUIRED, no defaults
 *   gen.id('######')           // "847293" - 6 digit number
 *   gen.id('XXX-####')         // "KWR-4821" - 3 letters, hyphen, 4 digits
 *   gen.id('##-####-####-##')  // "12-4829-3847-93" - formatted ID
 *
 *   // Card last 4 digits (optional type for realistic first digit)
 *   gen.card()                 // "4821" - random first digit
 *   gen.card('visa')           // "4xxx" (Visa starts with 4)
 *   gen.card('mastercard')     // "5xxx" (MC starts with 5)
 *   gen.card('amex')           // "3xxx" (Amex starts with 3)
 *
 *   // Full card details (number, CVV, expiry)
 *   const c = gen.cardFull('visa')
 *   c.number      // "4532015112830366"
 *   c.formatted   // "4532 0151 1283 0366"
 *   c.last4       // "0366"
 *   c.cvv         // "123" (4 digits for Amex)
 *   c.expiry      // "03/28"
 *   c.expiryMonth // "03"
 *   c.expiryYear  // "2028"
 *
 * Pattern characters:
 *   # = digit (0-9)
 *   X = uppercase letter (A-Z)
 *   x = lowercase letter (a-z)
 *   * = alphanumeric (0-9, A-Z)
 *   Anything else = literal (hyphens, spaces, etc.)
 *
 * Examples for different sites:
 *   Amazon order:    gen.id('###-#######-#######')
 *   Home Depot SKU:  gen.id('######')
 *   UPS tracking:    gen.id('1Z') + gen.id('******##########')
 *   Model number:    gen.id('XX####')
 */

// Simple seeded random number generator (mulberry32)
function createRng(seed) {
  let state = seed
  return function() {
    state |= 0
    state = state + 0x6D2B79F5 | 0
    let t = Math.imul(state ^ state >>> 15, 1 | state)
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t
    return ((t ^ t >>> 14) >>> 0) / 4294967296
  }
}

// Calculate Luhn check digit for valid card numbers
function calculateLuhnCheckDigit(partialNumber) {
  const digits = partialNumber.split('').map(Number).reverse()
  let sum = 0

  for (let i = 0; i < digits.length; i++) {
    let digit = digits[i]
    // Double every digit (since we're calculating for the check digit position)
    if (i % 2 === 0) {
      digit *= 2
      if (digit > 9) digit -= 9
    }
    sum += digit
  }

  // Check digit is what makes sum divisible by 10
  return String((10 - (sum % 10)) % 10)
}

// Convert string seed to number
function seedToNumber(seed) {
  if (typeof seed === 'number') return seed
  if (typeof seed === 'string') {
    let hash = 0
    for (let i = 0; i < seed.length; i++) {
      const char = seed.charCodeAt(i)
      hash = ((hash << 5) - hash) + char
      hash = hash & hash
    }
    return Math.abs(hash)
  }
  return 42 // default seed
}

/**
 * Create a generator instance with a specific seed.
 * All generated values are deterministic based on the seed.
 */
export function createGenerators(seed) {
  const numSeed = seedToNumber(seed)
  const rng = createRng(numSeed)

  // Track how many IDs generated (for variety within same seed)
  let idCounter = 0

  /**
   * Generate an ID based on a pattern.
   * @param {string} pattern - Pattern string (see module docs)
   * @returns {string} Generated ID
   */
  function id(pattern) {
    // Use counter to get different values for same pattern
    const localRng = createRng(numSeed + idCounter++)

    let result = ''
    for (const char of pattern) {
      switch (char) {
        case '#':
          result += Math.floor(localRng() * 10)
          break
        case 'X':
          result += String.fromCharCode(65 + Math.floor(localRng() * 26))
          break
        case 'x':
          result += String.fromCharCode(97 + Math.floor(localRng() * 26))
          break
        case '*':
          const alphaNum = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
          result += alphaNum[Math.floor(localRng() * alphaNum.length)]
          break
        default:
          result += char
      }
    }
    return result
  }

  /**
   * Generate last 4 digits of a card, optionally with correct prefix for card type.
   * @param {string} [cardType] - 'visa', 'mastercard', 'amex', 'discover'
   * @returns {string} Last 4 digits
   */
  function card(cardType) {
    const localRng = createRng(numSeed + 1000 + idCounter++)

    // Card type determines first digit of the 4 shown
    let firstDigit
    switch (cardType?.toLowerCase()) {
      case 'visa':
        firstDigit = '4'
        break
      case 'mastercard':
      case 'mc':
        firstDigit = '5'
        break
      case 'amex':
      case 'american express':
        firstDigit = '3'
        break
      case 'discover':
        firstDigit = '6'
        break
      default:
        // Random digit
        firstDigit = String(Math.floor(localRng() * 10))
    }

    // Generate remaining 3 digits
    const rest = String(Math.floor(localRng() * 1000)).padStart(3, '0')
    return firstDigit + rest
  }

  /**
   * Generate full card details (number, CVV, expiry).
   * @param {string} [cardType] - 'visa', 'mastercard', 'amex', 'discover'
   * @returns {object} { number, last4, cvv, expiry, expiryMonth, expiryYear }
   */
  function cardFull(cardType) {
    const localRng = createRng(numSeed + 2000 + idCounter++)

    // Card prefixes and lengths
    let prefix, length, cvvLength
    switch (cardType?.toLowerCase()) {
      case 'visa':
        prefix = '4'
        length = 16
        cvvLength = 3
        break
      case 'mastercard':
      case 'mc':
        // Mastercard: 51-55 or 2221-2720
        prefix = '5' + String(Math.floor(localRng() * 5) + 1)
        length = 16
        cvvLength = 3
        break
      case 'amex':
      case 'american express':
        // Amex: 34 or 37
        prefix = localRng() < 0.5 ? '34' : '37'
        length = 15
        cvvLength = 4
        break
      case 'discover':
        prefix = '6011'
        length = 16
        cvvLength = 3
        break
      default:
        // Default to Visa-like
        prefix = '4'
        length = 16
        cvvLength = 3
    }

    // Generate digits (all but last one)
    const remainingLength = length - prefix.length
    let number = prefix
    for (let i = 0; i < remainingLength - 1; i++) {
      number += String(Math.floor(localRng() * 10))
    }

    // Calculate Luhn check digit for valid card number
    number += calculateLuhnCheckDigit(number)

    // Generate CVV
    let cvv = ''
    for (let i = 0; i < cvvLength; i++) {
      cvv += String(Math.floor(localRng() * 10))
    }

    // Generate expiry (1-5 years in future)
    const currentYear = new Date().getFullYear()
    const expiryYear = currentYear + 1 + Math.floor(localRng() * 5)
    const expiryMonth = Math.floor(localRng() * 12) + 1
    const expiry = String(expiryMonth).padStart(2, '0') + '/' + String(expiryYear).slice(-2)

    return {
      number,                           // "4532015112830366"
      formatted: formatCardNumber(number), // "4532 0151 1283 0366"
      last4: number.slice(-4),          // "0366"
      cvv,                              // "123"
      expiry,                           // "03/28"
      expiryMonth: String(expiryMonth).padStart(2, '0'),  // "03"
      expiryYear: String(expiryYear),   // "2028"
    }
  }

  /**
   * Format card number with spaces (4-4-4-4 or 4-6-5 for Amex)
   */
  function formatCardNumber(number) {
    if (number.length === 15) {
      // Amex: 4-6-5
      return number.slice(0, 4) + ' ' + number.slice(4, 10) + ' ' + number.slice(10)
    }
    // Standard: 4-4-4-4
    return number.match(/.{1,4}/g)?.join(' ') || number
  }

  return {
    id,
    card,
    cardFull,
    // Expose raw RNG for custom use (returns 0-1)
    random: rng,
  }
}

// Default export for convenience
export default createGenerators
