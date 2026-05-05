import data from '@data'
import { createGenerators } from '@generators'
import { getPartialProps, getSelectProps } from './partialFill'

// Initialize generator with seed from data
const gen = createGenerators(data.SEED)

function App() {
  return (
    <div className="min-h-screen bg-gray-100 p-4">
      <h1 className="text-2xl font-bold">UI Reproduction Placeholder</h1>
      <p>Replace this component with the reproduced UI.</p>

      {/* Example generator usage - delete when replacing */}
      {/*
        // IDs with patterns (# = digit, X = letter, * = alphanumeric)
        gen.id('######')              // 6-digit number
        gen.id('###-#######-#######') // Amazon-style order ID
        gen.id('XX####')              // Model number style

        // Card last 4 (optional type: 'visa', 'mastercard', 'amex')
        gen.card('visa')              // "4xxx"

        // Full card details
        const c = gen.cardFull('visa')
        c.number      // "4532015112830366"
        c.formatted   // "4532 0151 1283 0366"
        c.last4       // "0366"
        c.cvv         // "123"
        c.expiry      // "03/28"

        // Use in JSX with data attributes:
        <span data-order="ORDER_ID">{gen.id('###-#######-#######')}</span>
        <span data-pii="PII_CARD_LAST4">****{gen.card('visa')}</span>
        <span data-pii="PII_CARD_NUMBER">{gen.cardFull('visa').formatted}</span>
        <span data-pii="PII_CARD_CVV">{gen.cardFull('visa').cvv}</span>
        <span data-pii="PII_CARD_EXPIRY">{gen.cardFull('visa').expiry}</span>

        // Partial fill helpers - for input fields that may show truncated values
        // Use getFieldValue(key) for input values, data[key] for display text
        <input data-pii="PII_EMAIL" {...getPartialProps('PII_EMAIL')} />
      */}
    </div>
  )
}

export default App
