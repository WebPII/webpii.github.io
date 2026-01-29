/**
 * WebPII Dataset Viewer
 * Interactive viewer for browsing annotated e-commerce UI samples
 */

class DatasetViewer {
  constructor(containerId) {
    this.container = document.getElementById(containerId);
    this.samples = [];
    this.currentSampleIndex = 0;
    this.state = {
      fillState: 'full', // 'full' | 'partial' | 'empty'
      showAnnotations: false
    };
    this.metadata = null;

    this.init();
  }

  async init() {
    try {
      // Load samples index
      const response = await fetch('static/data/samples-index.json');
      const data = await response.json();
      this.samples = data.samples;

      // Build UI
      this.buildUI();

      // Load first sample
      await this.loadSample(0);

      // Setup keyboard navigation
      this.setupKeyboardNavigation();

    } catch (error) {
      console.error('Failed to initialize dataset viewer:', error);
      this.showError('Failed to load dataset samples');
    }
  }

  buildUI() {
    this.container.innerHTML = `
      <div class="viewer-container">
        <!-- Sample Selector -->
        <div class="viewer-header">
          <div class="sample-selector">
            <label for="sample-dropdown">Select Sample:</label>
            <select id="sample-dropdown" class="sample-dropdown">
              ${this.samples.map((sample, idx) => `
                <option value="${idx}">${sample.displayName}</option>
              `).join('')}
            </select>
          </div>
          <div class="viewer-nav-buttons">
            <button id="prev-sample" class="nav-button" title="Previous sample (←)">← Previous</button>
            <button id="next-sample" class="nav-button" title="Next sample (→)">Next →</button>
          </div>
        </div>

        <!-- Main Viewer -->
        <div class="viewer-main">
          <!-- Controls Panel -->
          <div class="viewer-controls">
            <div class="control-group">
              <h3>Form State</h3>
              <label class="radio-label">
                <input type="radio" name="fillState" value="full" checked>
                <span>Full</span>
              </label>
              <label class="radio-label">
                <input type="radio" name="fillState" value="partial">
                <span>Partial</span>
              </label>
              <label class="radio-label">
                <input type="radio" name="fillState" value="empty">
                <span>Empty</span>
              </label>
            </div>

            <div class="control-group">
              <h3>Annotations</h3>
              <label class="checkbox-label">
                <input type="checkbox" id="show-annotations">
                <span>Show Bounding Boxes</span>
              </label>
            </div>

            <div class="control-group sample-info">
              <h3>Sample Info</h3>
              <div id="sample-details"></div>
            </div>
          </div>

          <!-- Image Display -->
          <div class="viewer-center-column">
            <div class="viewer-image-container">
              <div id="loading-indicator" class="loading-indicator">Loading...</div>
              <div id="error-indicator" class="error-indicator" style="display: none;"></div>
              <div class="image-wrapper" style="display: none;">
                <img id="viewer-image" class="viewer-image" alt="Dataset sample">
                <canvas id="bbox-canvas" class="bbox-canvas"></canvas>
              </div>
              <div id="variant-warning" class="variant-warning" style="display: none;"></div>
            </div>

            <!-- Keyboard Shortcuts -->
            <div class="hotkeys-info">
              <div class="hotkeys-list">
                <span class="hotkey-item"><kbd>←/→</kbd> Navigate samples</span>
                <span class="hotkey-item"><kbd>↑/↓</kbd> Navigate elements</span>
                <span class="hotkey-item"><kbd>1/2/3</kbd> Form states</span>
                <span class="hotkey-item"><kbd>A</kbd> Toggle annotations</span>
                <span class="hotkey-item"><kbd>Esc</kbd> Clear highlight</span>
              </div>
            </div>
          </div>

          <!-- Metadata Panel -->
          <div class="viewer-metadata">
            <h3>Detected Elements</h3>
            <div id="metadata-content"></div>
          </div>
        </div>
      </div>
    `;

    // Attach event listeners
    this.attachEventListeners();
  }

  attachEventListeners() {
    // Sample selector
    document.getElementById('sample-dropdown').addEventListener('change', (e) => {
      this.loadSample(parseInt(e.target.value));
    });

    // Navigation buttons
    document.getElementById('prev-sample').addEventListener('click', () => {
      this.navigateSample(-1);
    });
    document.getElementById('next-sample').addEventListener('click', () => {
      this.navigateSample(1);
    });

    // Form state radios
    document.querySelectorAll('input[name="fillState"]').forEach(radio => {
      radio.addEventListener('change', (e) => {
        this.state.fillState = e.target.value;
        this.updateImage();
      });
    });

    // Annotations checkbox
    document.getElementById('show-annotations').addEventListener('change', (e) => {
      this.state.showAnnotations = e.target.checked;
      // Update form state controls when annotation state changes
      const sample = this.samples[this.currentSampleIndex];
      this.updateFormStateControls(sample);
      this.updateImage();
    });
  }

  setupKeyboardNavigation() {
    this.currentHighlightIndex = -1;
    this.allClickableElements = [];

    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') {
        return; // Don't interfere with form inputs
      }

      switch(e.key) {
        case 'ArrowLeft':
          e.preventDefault();
          this.navigateSample(-1);
          break;
        case 'ArrowRight':
          e.preventDefault();
          this.navigateSample(1);
          break;
        case 'ArrowUp':
          e.preventDefault();
          this.navigateElement(-1);
          break;
        case 'ArrowDown':
          e.preventDefault();
          this.navigateElement(1);
          break;
        case '1':
          e.preventDefault();
          this.setFormState('full');
          break;
        case '2':
          e.preventDefault();
          this.setFormState('partial');
          break;
        case '3':
          e.preventDefault();
          this.setFormState('empty');
          break;
        case 'a':
        case 'A':
          e.preventDefault();
          this.toggleAnnotations();
          break;
        case 'Escape':
          e.preventDefault();
          this.clearHighlight();
          break;
      }
    });
  }

  navigateElement(direction) {
    // Refresh list of clickable elements
    this.allClickableElements = Array.from(
      document.querySelectorAll('.pii-field.clickable, .product-item.clickable, .order-item.clickable, .order-field.clickable, .misc-field.clickable')
    );

    if (this.allClickableElements.length === 0) return;

    // Update index
    this.currentHighlightIndex += direction;

    // Wrap around
    if (this.currentHighlightIndex < 0) {
      this.currentHighlightIndex = this.allClickableElements.length - 1;
    } else if (this.currentHighlightIndex >= this.allClickableElements.length) {
      this.currentHighlightIndex = 0;
    }

    // Highlight the element
    const element = this.allClickableElements[this.currentHighlightIndex];
    const bboxId = element.getAttribute('data-bbox-id');

    // Scroll element into view in sidebar
    element.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    // Highlight bbox on image
    this.highlightBBox(bboxId);
  }

  setFormState(state) {
    const radio = document.querySelector(`input[name="fillState"][value="${state}"]`);
    if (radio && !radio.disabled) {
      radio.checked = true;
      this.state.fillState = state;
      this.updateImage();
    }
  }

  toggleAnnotations() {
    const checkbox = document.getElementById('show-annotations');
    checkbox.checked = !checkbox.checked;
    this.state.showAnnotations = checkbox.checked;
    const sample = this.samples[this.currentSampleIndex];
    this.updateFormStateControls(sample);
    this.updateImage();
  }

  clearHighlight() {
    this.clearCanvas();
    this.currentHighlightIndex = -1;

    // Clear active card highlighting
    const allCards = document.querySelectorAll('.active-card');
    allCards.forEach(card => card.classList.remove('active-card'));
  }

  navigateSample(direction) {
    const newIndex = this.currentSampleIndex + direction;
    if (newIndex >= 0 && newIndex < this.samples.length) {
      this.loadSample(newIndex);
      document.getElementById('sample-dropdown').value = newIndex;
    }
  }

  async loadSample(index) {
    this.currentSampleIndex = index;
    const sample = this.samples[index];

    try {
      // Load metadata
      const metadataPath = `static/samples/${sample.id}/metadata.json`;
      const response = await fetch(metadataPath);
      this.metadata = await response.json();

      // Update form state controls based on available variants
      this.updateFormStateControls(sample);

      // Update displays
      this.updateSampleInfo(sample);
      this.updateMetadataDisplay();
      this.updateImage();

    } catch (error) {
      console.error('Failed to load sample:', error);
      this.showError(`Failed to load sample: ${sample.displayName}`);
    }
  }

  updateFormStateControls(sample) {
    const fullRadio = document.querySelector('input[name="fillState"][value="full"]');
    const partialRadio = document.querySelector('input[name="fillState"][value="partial"]');
    const emptyRadio = document.querySelector('input[name="fillState"][value="empty"]');

    // Check if annotations are enabled
    const showAnnotations = this.state.showAnnotations;

    // Determine availability based on current annotation state
    const fullAvailable = showAnnotations ? sample.hasFull : sample.hasFullClean;
    const partialAvailable = showAnnotations ? sample.hasPartial : sample.hasPartialClean;
    const emptyAvailable = showAnnotations ? sample.hasEmpty : sample.hasEmptyClean;

    // Enable/disable radios
    fullRadio.disabled = !fullAvailable;
    fullRadio.parentElement.style.opacity = fullAvailable ? '1' : '0.5';

    partialRadio.disabled = !partialAvailable;
    partialRadio.parentElement.style.opacity = partialAvailable ? '1' : '0.5';

    emptyRadio.disabled = !emptyAvailable;
    emptyRadio.parentElement.style.opacity = emptyAvailable ? '1' : '0.5';

    // If current state is not available, switch to first available
    const currentState = this.state.fillState;
    const currentAvailable =
      (currentState === 'full' && fullAvailable) ||
      (currentState === 'partial' && partialAvailable) ||
      (currentState === 'empty' && emptyAvailable);

    if (!currentAvailable) {
      if (fullAvailable) {
        this.state.fillState = 'full';
        fullRadio.checked = true;
      } else if (partialAvailable) {
        this.state.fillState = 'partial';
        partialRadio.checked = true;
      } else if (emptyAvailable) {
        this.state.fillState = 'empty';
        emptyRadio.checked = true;
      }
    }
  }

  updateSampleInfo(sample) {
    const detailsEl = document.getElementById('sample-details');
    detailsEl.innerHTML = `
      <p><strong>Company:</strong> ${this.formatCompanyName(sample.company)}</p>
      <p><strong>Page Type:</strong> ${this.formatPageType(sample.pageType)}</p>
    `;
  }

  updateMetadataDisplay() {
    const metadataEl = document.getElementById('metadata-content');

    if (!this.metadata) {
      metadataEl.innerHTML = '<p class="no-data">No metadata available</p>';
      return;
    }

    const dataJson = this.metadata.data_json || {};
    const requiredFields = this.metadata.required_fields || [];
    const detectionStats = this.metadata.detection_stats || {};
    const piiElements = this.metadata.pii_elements || [];
    const allProductElements = this.metadata.product_elements || [];
    const searchElements = this.metadata.search_elements || [];

    // Separate ORDER, CART, PRODUCT, and SEARCH elements from product_elements
    // Note: product_elements can contain SEARCH items (SEARCH_*, HEADER_SEARCH)
    const orderElements = allProductElements.filter(el => el.key.startsWith('ORDER'));
    const cartElements = allProductElements.filter(el => el.key.startsWith('CART'));
    const productElements = allProductElements.filter(el => el.key.startsWith('PRODUCT'));
    const searchElementsFromProduct = allProductElements.filter(el =>
      el.key.startsWith('SEARCH') || el.key === 'HEADER_SEARCH'
    );

    // Combine search elements from both sources
    const allSearchElements = [...searchElements, ...searchElementsFromProduct];

    // Filter fields from required_fields (excluding SEED) and also from data_json
    const piiFields = {};
    const orderFields = {};
    const cartFields = {};
    const productFields = {};
    const searchFields = {};
    const miscFields = {};

    // First, add all fields from required_fields
    for (const fieldKey of requiredFields) {
      if (fieldKey === 'SEED') continue; // Skip SEED

      const hasValue = dataJson[fieldKey] !== undefined && dataJson[fieldKey] !== null && dataJson[fieldKey] !== '';
      const value = hasValue ? dataJson[fieldKey] : '(empty)';

      if (fieldKey.startsWith('PII_')) {
        // Only include PII fields if they have actual values
        if (hasValue) {
          piiFields[fieldKey] = value;
        }
      } else if (fieldKey.startsWith('ORDER_')) {
        orderFields[fieldKey] = value;
      } else if (fieldKey.startsWith('CART_')) {
        cartFields[fieldKey] = value;
      } else if (fieldKey.startsWith('PRODUCT')) {
        productFields[fieldKey] = value;
      } else if (fieldKey.startsWith('SEARCH_')) {
        searchFields[fieldKey] = value;
      } else {
        miscFields[fieldKey] = value;
      }
    }

    // Also add ORDER/CART/SEARCH fields from data_json that aren't in required_fields
    for (const [key, value] of Object.entries(dataJson)) {
      if (value === undefined || value === null || value === '') continue;

      // Skip if already added from required_fields
      if (requiredFields.includes(key)) continue;

      if (key.startsWith('ORDER_')) {
        orderFields[key] = value;
      } else if (key.startsWith('CART_')) {
        cartFields[key] = value;
      } else if (key.startsWith('SEARCH_')) {
        searchFields[key] = value;
      }
    }

    // Also add ORDER/CART fields from product_elements that have values
    // OVERWRITE "(empty)" values with actual values from elements
    for (const element of allProductElements) {
      const key = element.key;
      const value = element.value;

      // Skip if no value
      if (!value) continue;

      // Only add standalone ORDER/CART fields (not numbered ones like ORDER1_ID)
      if (key.startsWith('ORDER_') && !key.match(/^ORDER\d+_/)) {
        orderFields[key] = value;  // Overwrite even if already exists
      } else if (key.startsWith('CART_') && !key.match(/^CART\d+_/)) {
        cartFields[key] = value;  // Overwrite even if already exists
      }
    }

    // Also add SEARCH fields from search_elements (both sources)
    // Include even if no value (will show as empty), as long as they have bounding boxes
    for (const element of allSearchElements) {
      const key = element.key;
      const value = element.value || '';

      // Skip if already added
      if (searchFields[key]) continue;

      // Add SEARCH fields and HEADER_SEARCH (not numbered ones like SEARCH1_QUERY)
      if ((key.startsWith('SEARCH_') && !key.match(/^SEARCH\d+_/)) || key === 'HEADER_SEARCH') {
        searchFields[key] = value || '(empty)';
      }
    }

    // Also add MISC fields from product_elements (anything that's not categorized above)
    for (const element of allProductElements) {
      const key = element.key;
      const value = element.value;

      // Skip if no value or already added
      if (!value) continue;
      if (miscFields[key]) continue;

      // Add if it's not ORDER, CART, PRODUCT, or SEARCH
      const isKnownType =
        key.startsWith('ORDER') ||
        key.startsWith('CART') ||
        key.startsWith('PRODUCT') ||
        key.startsWith('SEARCH') ||
        key === 'HEADER_SEARCH';

      if (!isKnownType) {
        miscFields[key] = value;
      }
    }

    const piiCount = Object.keys(piiFields).length;
    let html = '';

    // Display PII Fields - ALL fields with visible bounding boxes, sorted by position
    // Build complete piiFields from both required_fields AND pii_elements
    const allPIIFields = { ...piiFields };
    for (const element of piiElements) {
      if (element.visible && element.key && !allPIIFields[element.key]) {
        allPIIFields[element.key] = element.value || '(empty)';
      }
    }

    if (Object.keys(allPIIFields).length > 0) {
      const categories = this.categorizePII(allPIIFields);
      let totalVisiblePII = 0;
      let categoriesHtml = '';

      for (const [category, fields] of Object.entries(categories)) {
        // Filter to only fields with visible bounding boxes and attach bbox info
        const visibleFields = [];
        for (const [key, value] of fields) {
          const matchingElements = piiElements.filter(el => el.key === key && el.visible);
          if (matchingElements.length > 0) {
            visibleFields.push({ key, value, matchingElements });
            totalVisiblePII++;
          }
        }

        if (visibleFields.length === 0) continue;

        // Sort by position (top to bottom, left to right)
        visibleFields.sort((a, b) => {
          const bboxA = a.matchingElements[0].bbox;
          const bboxB = b.matchingElements[0].bbox;
          // Primary sort: y position (top to bottom)
          if (Math.abs(bboxA.y - bboxB.y) > 10) {
            return bboxA.y - bboxB.y;
          }
          // Secondary sort: x position (left to right)
          return bboxA.x - bboxB.x;
        });

        categoriesHtml += `<div class="pii-category"><h4>${category}</h4><div class="pii-fields">`;

        for (const field of visibleFields) {
          const isEmpty = field.value === '(empty)';
          const bboxId = `pii-${field.key}`;

          categoriesHtml += `
            <div class="pii-field clickable" data-bbox-id="${bboxId}">
              <span class="pii-key">${this.formatPIIKey(field.key)}:</span>
              <span class="pii-value ${isEmpty ? 'empty' : ''}">${this.truncateValue(field.value)}</span>
            </div>
          `;
        }

        categoriesHtml += '</div></div>';
      }

      if (totalVisiblePII > 0) {
        html += `<div class="pii-summary"><strong>${totalVisiblePII} PII Fields</strong></div>`;
        html += categoriesHtml;
      }
    }

    // Display Order/Cart Fields - only those with visible bounding boxes
    const orderCartFields = { ...orderFields, ...cartFields };
    const visibleOrderCartFields = [];

    for (const [key, value] of Object.entries(orderCartFields)) {
      // Check if this field has visible bounding boxes in product_elements
      const matchingElements = [...orderElements, ...cartElements].filter(el =>
        el.visible && (el.key === key || el.key.startsWith(key.replace(/_/g, '')))
      );

      if (matchingElements.length > 0) {
        visibleOrderCartFields.push({ key, value, matchingElements });
      }
    }

    if (visibleOrderCartFields.length > 0) {
      // Sort by position (top to bottom, left to right)
      visibleOrderCartFields.sort((a, b) => {
        const bboxA = a.matchingElements[0].bbox;
        const bboxB = b.matchingElements[0].bbox;
        // Primary sort: y position (top to bottom)
        if (Math.abs(bboxA.y - bboxB.y) > 10) {
          return bboxA.y - bboxB.y;
        }
        // Secondary sort: x position (left to right)
        return bboxA.x - bboxB.x;
      });

      html += `<div class="order-summary"><strong>${visibleOrderCartFields.length} Order/Cart Fields</strong></div>`;
      html += '<div class="order-category"><h4>Order & Cart Information</h4><div class="order-fields">';

      for (const field of visibleOrderCartFields) {
        const isEmpty = field.value === '(empty)';
        const bboxId = `field-${field.key}`;

        html += `
          <div class="order-field clickable" data-bbox-id="${bboxId}">
            <span class="order-key">${this.formatFieldKey(field.key)}:</span>
            <span class="order-value ${isEmpty ? 'empty' : ''}">${this.truncateValue(field.value)}</span>
          </div>
        `;
      }

      html += '</div></div>';
    }

    // Display Grouped Orders (ORDER1_, ORDER2_, etc.)
    const numberedOrderElements = orderElements.filter(el => el.key.match(/^ORDER\d+_/));
    if (numberedOrderElements.length > 0) {
      const groupedOrders = this.groupOrders(numberedOrderElements);

      if (groupedOrders.length > 0) {
        html += `<div class="order-summary"><strong>${groupedOrders.length} Orders</strong></div>`;
        html += '<div class="order-category"><h4>Order Details</h4><div class="order-items">';

        for (let i = 0; i < groupedOrders.length; i++) {
          const order = groupedOrders[i];
          const orderId = `order-${i + 1}`;

          html += `
            <div class="order-item clickable" data-bbox-id="${orderId}">
              <div class="order-header">Order ${i + 1}</div>`;

          // Display all fields dynamically
          for (const [fieldKey, fieldValue] of Object.entries(order.fields)) {
            if (fieldValue) {
              html += `<div class="order-detail"><strong>${this.formatFieldKey(fieldKey)}:</strong> ${this.truncateValue(fieldValue)}</div>`;
            }
          }

          html += `
            </div>
          `;
        }

        html += '</div></div>';
      }
    }

    // Display Search Fields - only those with visible bounding boxes
    const visibleSearchFields = [];

    for (const [key, value] of Object.entries(searchFields)) {
      // Check if this field has visible bounding boxes in allSearchElements
      const matchingElements = allSearchElements.filter(el =>
        el.visible && (el.key === key || el.key.startsWith(key.replace(/_/g, '')))
      );

      if (matchingElements.length > 0) {
        visibleSearchFields.push({ key, value, matchingElements });
      }
    }

    if (visibleSearchFields.length > 0) {
      // Sort by position (top to bottom, left to right)
      visibleSearchFields.sort((a, b) => {
        const bboxA = a.matchingElements[0].bbox;
        const bboxB = b.matchingElements[0].bbox;
        // Primary sort: y position (top to bottom)
        if (Math.abs(bboxA.y - bboxB.y) > 10) {
          return bboxA.y - bboxB.y;
        }
        // Secondary sort: x position (left to right)
        return bboxA.x - bboxB.x;
      });

      html += `<div class="order-summary" style="background: #f3e5f5; color: #6a1b9a;"><strong>${visibleSearchFields.length} Search Fields</strong></div>`;
      html += '<div class="order-category"><h4>Search Information</h4><div class="order-fields">';

      for (const field of visibleSearchFields) {
        const isEmpty = field.value === '(empty)';
        const bboxId = `field-${field.key}`;

        html += `
          <div class="order-field clickable" data-bbox-id="${bboxId}" style="border-left-color: #9c27b0;">
            <span class="order-key">${this.formatFieldKey(field.key)}:</span>
            <span class="order-value ${isEmpty ? 'empty' : ''}">${this.truncateValue(field.value)}</span>
          </div>
        `;
      }

      html += '</div></div>';
    }


    // Display Product Elements
    if (productElements.length > 0) {
      const products = this.groupProducts(productElements);

      html += `<div class="product-summary"><strong>${products.length} Products</strong></div>`;
      html += '<div class="product-category"><h4>Product Details</h4><div class="product-items">';

      for (let i = 0; i < products.length; i++) {
        const product = products[i];
        const productId = `product-${i + 1}`;

        html += `
          <div class="product-item clickable" data-bbox-id="${productId}">
            <div class="product-header">Product ${i + 1}</div>
            ${product.name ? `<div class="product-detail"><strong>Name:</strong> ${this.truncateValue(product.name, 60)}</div>` : ''}
            ${product.price ? `<div class="product-detail"><strong>Price:</strong> $${product.price}</div>` : ''}
            ${product.quantity ? `<div class="product-detail"><strong>Quantity:</strong> ${product.quantity}</div>` : ''}
          </div>
        `;
      }

      html += '</div></div>';
    }


    // Display Misc Fields - only those with visible bounding boxes
    const visibleMiscFields = [];

    for (const [key, value] of Object.entries(miscFields)) {
      // Check if this field has visible bounding boxes in product_elements
      const matchingElements = allProductElements.filter(el =>
        el.visible && el.key === key
      );

      if (matchingElements.length > 0) {
        visibleMiscFields.push({ key, value, matchingElements });
      }
    }

    if (visibleMiscFields.length > 0) {
      // Sort by position (top to bottom, left to right)
      visibleMiscFields.sort((a, b) => {
        const bboxA = a.matchingElements[0].bbox;
        const bboxB = b.matchingElements[0].bbox;
        // Primary sort: y position (top to bottom)
        if (Math.abs(bboxA.y - bboxB.y) > 10) {
          return bboxA.y - bboxB.y;
        }
        // Secondary sort: x position (left to right)
        return bboxA.x - bboxB.x;
      });

      html += `<div class="misc-summary"><strong>${visibleMiscFields.length} Other Fields</strong></div>`;
      html += '<div class="misc-category"><h4>Miscellaneous</h4><div class="misc-fields">';

      for (const field of visibleMiscFields) {
        const isEmpty = field.value === '(empty)';
        const bboxId = `field-${field.key}`;

        html += `
          <div class="misc-field clickable" data-bbox-id="${bboxId}">
            <span class="misc-key">${this.formatFieldKey(field.key)}:</span>
            <span class="misc-value ${isEmpty ? 'empty' : ''}">${this.truncateValue(field.value)}</span>
          </div>
        `;
      }

      html += '</div></div>';
    }

    if (piiCount === 0 && Object.keys(orderCartFields).length === 0 && Object.keys(searchFields).length === 0 &&
        productElements.length === 0 && Object.keys(miscFields).length === 0) {
      html += '<p class="no-data">No data in this sample</p>';
    }

    metadataEl.innerHTML = html;

    // Attach click handlers to clickable cards
    this.attachBBoxClickHandlers();
  }

  groupProducts(productElements) {
    const products = {};

    for (const element of productElements) {
      if (!element.visible) continue;

      const match = element.key.match(/^PRODUCT(\d+)_(.+)$/);
      if (!match) continue;

      const productNum = match[1];
      const field = match[2];

      if (!products[productNum]) {
        products[productNum] = { elements: [] };
      }

      products[productNum].elements.push(element);

      if (field === 'NAME') {
        products[productNum].name = element.value;
      } else if (field === 'PRICE') {
        products[productNum].price = element.value;
      } else if (field === 'QUANTITY') {
        products[productNum].quantity = element.value;
      }
    }

    // Convert to array and sort by position (top to bottom, left to right)
    const productArray = Object.values(products);
    productArray.sort((a, b) => {
      const bboxA = a.elements[0].bbox;
      const bboxB = b.elements[0].bbox;
      // Primary sort: y position (top to bottom)
      if (Math.abs(bboxA.y - bboxB.y) > 10) {
        return bboxA.y - bboxB.y;
      }
      // Secondary sort: x position (left to right)
      return bboxA.x - bboxB.x;
    });

    return productArray;
  }

  groupOrders(orderElements) {
    const orders = {};

    for (const element of orderElements) {
      if (!element.visible) continue;

      const match = element.key.match(/^ORDER(\d+)_(.+)$/);
      if (!match) continue;

      const orderNum = match[1];
      const field = match[2];

      if (!orders[orderNum]) {
        orders[orderNum] = { elements: [], fields: {} };
      }

      orders[orderNum].elements.push(element);
      // Store all fields dynamically
      orders[orderNum].fields[field] = element.value;
    }

    // Convert to array and sort by position (top to bottom, left to right)
    const orderArray = Object.values(orders);
    orderArray.sort((a, b) => {
      const bboxA = a.elements[0].bbox;
      const bboxB = b.elements[0].bbox;
      // Primary sort: y position (top to bottom)
      if (Math.abs(bboxA.y - bboxB.y) > 10) {
        return bboxA.y - bboxB.y;
      }
      // Secondary sort: x position (left to right)
      return bboxA.x - bboxB.x;
    });

    return orderArray;
  }

  formatFieldKey(key) {
    return key.replace(/^(PII_|ORDER_|CART_|PRODUCT|SEARCH_)/, '').replace(/_/g, ' ').toLowerCase()
      .replace(/\b\w/g, l => l.toUpperCase());
  }

  attachBBoxClickHandlers() {
    const clickableCards = document.querySelectorAll('.pii-field.clickable, .product-item.clickable, .order-item.clickable, .order-field.clickable, .misc-field.clickable');

    clickableCards.forEach(card => {
      card.addEventListener('click', () => {
        const bboxId = card.getAttribute('data-bbox-id');
        this.highlightBBox(bboxId);
      });
    });
  }

  highlightBBox(bboxId) {
    if (!bboxId || !this.metadata) return;

    // Highlight the card in the sidebar
    this.highlightCard(bboxId);

    const piiElements = this.metadata.pii_elements || [];
    const allProductElements = this.metadata.product_elements || [];
    const searchElements = this.metadata.search_elements || [];

    const orderElements = allProductElements.filter(el => el.key.startsWith('ORDER'));
    const cartElements = allProductElements.filter(el => el.key.startsWith('CART'));
    const productElements = allProductElements.filter(el => el.key.startsWith('PRODUCT'));
    const searchElementsFromProduct = allProductElements.filter(el =>
      el.key.startsWith('SEARCH') || el.key === 'HEADER_SEARCH'
    );

    // Combine search elements from both sources
    const allSearchElements = [...searchElements, ...searchElementsFromProduct];

    let elementsToHighlight = [];

    if (bboxId.startsWith('pii-')) {
      const piiKey = bboxId.replace('pii-', '');
      elementsToHighlight = piiElements.filter(el => el.key === piiKey && el.visible);
    } else if (bboxId.startsWith('field-')) {
      // For order/cart/search fields
      const fieldKey = bboxId.replace('field-', '');

      // Check ORDER/CART in product_elements
      if (fieldKey.startsWith('ORDER_') || fieldKey.startsWith('CART_')) {
        elementsToHighlight = [...orderElements, ...cartElements].filter(el =>
          (el.key === fieldKey || el.key.startsWith(fieldKey.replace(/_/g, ''))) && el.visible
        );
      }
      // Check SEARCH in allSearchElements (both search_elements and product_elements)
      else if (fieldKey.startsWith('SEARCH_') || fieldKey === 'HEADER_SEARCH') {
        elementsToHighlight = allSearchElements.filter(el =>
          (el.key === fieldKey || el.key.startsWith(fieldKey.replace(/_/g, ''))) && el.visible
        );
      }
      // Check MISC fields in product_elements
      else {
        elementsToHighlight = allProductElements.filter(el =>
          el.key === fieldKey && el.visible
        );
      }
    } else if (bboxId.startsWith('product-')) {
      const productNum = bboxId.replace('product-', '');
      elementsToHighlight = productElements.filter(el =>
        el.key.startsWith(`PRODUCT${productNum}_`) && el.visible
      );
    } else if (bboxId.startsWith('order-')) {
      const orderNum = bboxId.replace('order-', '');
      // Get numbered order elements (ORDER1_, ORDER2_, etc.)
      const numberedOrderElements = orderElements.filter(el => el.key.match(/^ORDER\d+_/));
      const groupedOrders = this.groupOrders(numberedOrderElements);

      if (groupedOrders[orderNum - 1]) {
        elementsToHighlight = groupedOrders[orderNum - 1].elements.filter(el => el.visible);
      }
    }

    if (elementsToHighlight.length === 0) return;

    this.drawBBoxes(elementsToHighlight, true);
  }

  highlightCard(bboxId) {
    // Remove active class from all cards
    const allCards = document.querySelectorAll('.pii-field.clickable, .product-item.clickable, .order-item.clickable, .order-field.clickable, .misc-field.clickable');
    allCards.forEach(card => card.classList.remove('active-card'));

    // Add active class to the clicked card
    const activeCard = document.querySelector(`[data-bbox-id="${bboxId}"]`);
    if (activeCard) {
      activeCard.classList.add('active-card');
    }
  }

  categorizePII(piiFields) {
    const categories = {
      'Personal Info': [],
      'Contact': [],
      'Address': [],
      'Payment': [],
      'Account': [],
      'Other': []
    };

    for (const [key, value] of Object.entries(piiFields)) {
      const keyLower = key.toLowerCase();

      if (keyLower.includes('name') || keyLower.includes('dob')) {
        categories['Personal Info'].push([key, value]);
      } else if (keyLower.includes('email') || keyLower.includes('phone')) {
        categories['Contact'].push([key, value]);
      } else if (keyLower.includes('address') || keyLower.includes('street') ||
                 keyLower.includes('city') || keyLower.includes('state') ||
                 keyLower.includes('zip') || keyLower.includes('postcode') ||
                 keyLower.includes('country')) {
        categories['Address'].push([key, value]);
      } else if (keyLower.includes('card') || keyLower.includes('payment')) {
        categories['Payment'].push([key, value]);
      } else if (keyLower.includes('login') || keyLower.includes('username') ||
                 keyLower.includes('password')) {
        categories['Account'].push([key, value]);
      } else {
        categories['Other'].push([key, value]);
      }
    }

    return categories;
  }

  formatPIIKey(key) {
    return key.replace('PII_', '').replace(/_/g, ' ').toLowerCase()
      .replace(/\b\w/g, l => l.toUpperCase());
  }

  formatCompanyName(company) {
    const names = {
      'macys': "Macy's",
      'amazon': 'Amazon',
      'slack': 'Slack',
      'home-depot': 'Home Depot',
      'walmart': 'Walmart',
      'ulta-beauty': 'Ulta Beauty',
      'lowes': "Lowe's",
      'apple': 'Apple',
      'bh-photo': 'B&H Photo',
      'crate-barrel': 'Crate & Barrel'
    };
    return names[company] || company;
  }

  formatPageType(pageType) {
    return pageType.split('-').map(word =>
      word.charAt(0).toUpperCase() + word.slice(1)
    ).join(' ');
  }

  truncateValue(value, maxLength = 50) {
    if (typeof value !== 'string') {
      value = String(value);
    }
    if (value.length <= maxLength) return value;
    return value.substring(0, maxLength) + '...';
  }

  updateImage() {
    const sample = this.samples[this.currentSampleIndex];
    const imagePath = this.getImagePath(sample);

    if (!imagePath) {
      this.showVariantWarning(sample);
      return;
    }

    this.hideError();
    this.hideVariantWarning();
    this.showLoading();

    const img = document.getElementById('viewer-image');
    const wrapper = document.querySelector('.image-wrapper');
    const canvas = document.getElementById('bbox-canvas');

    img.onload = () => {
      this.hideLoading();
      wrapper.style.display = 'block';

      // Setup canvas for potential highlights when clicking
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      this.clearCanvas();
    };
    img.onerror = () => {
      this.hideLoading();
      this.showError('Failed to load image');
    };
    img.src = imagePath;
  }


  drawBBoxes(elements, highlight = false) {
    const canvas = document.getElementById('bbox-canvas');
    const ctx = canvas.getContext('2d');

    // Clear canvas
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    if (elements.length === 0 || !highlight) return;

    // Draw highlighted elements only
    elements.forEach((element, index) => {
      const { x, y, width, height } = element.bbox;

      // Highlight color
      ctx.strokeStyle = '#ff5722';
      ctx.lineWidth = 4;
      ctx.strokeRect(x, y, width, height);

      // Fill with semi-transparent color
      ctx.fillStyle = 'rgba(255, 87, 34, 0.15)';
      ctx.fillRect(x, y, width, height);
    });
  }

  clearCanvas() {
    const canvas = document.getElementById('bbox-canvas');
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  getImagePath(sample) {
    const { fillState, showAnnotations } = this.state;
    const baseDir = `static/samples/${sample.id}`;
    const baseName = sample.id;

    // Determine desired variant
    let desiredPath = null;

    if (fillState === 'full') {
      if (showAnnotations) {
        // Full + Annotated
        if (sample.hasFull) {
          desiredPath = `${baseDir}/${baseName}.png`;
        }
      } else {
        // Full + Clean
        if (sample.hasFullClean) {
          desiredPath = `${baseDir}/${baseName}_clean.png`;
        }
      }
    } else if (fillState === 'partial') {
      if (showAnnotations) {
        // Partial + Annotated
        if (sample.hasPartial) {
          desiredPath = `${baseDir}/${baseName}_partial.png`;
        }
      } else {
        // Partial + Clean
        if (sample.hasPartialClean) {
          desiredPath = `${baseDir}/${baseName}_partial_clean.png`;
        }
      }
    } else if (fillState === 'empty') {
      if (showAnnotations) {
        // Empty + Annotated
        if (sample.hasEmpty) {
          desiredPath = `${baseDir}/${baseName}_empty.png`;
        }
      } else {
        // Empty + Clean
        if (sample.hasEmptyClean) {
          desiredPath = `${baseDir}/${baseName}_empty_clean.png`;
        }
      }
    }

    // Fallback to available variant
    if (!desiredPath) {
      // Try full clean as fallback
      if (sample.hasFullClean) {
        desiredPath = `${baseDir}/${baseName}_clean.png`;
      } else if (sample.hasEmptyClean) {
        desiredPath = `${baseDir}/${baseName}_empty_clean.png`;
      }

      if (desiredPath) {
        this.showVariantWarning(sample);
      }
    }

    return desiredPath;
  }

  showVariantWarning(sample) {
    const warningEl = document.getElementById('variant-warning');
    const { fillState, showAnnotations } = this.state;

    let message = `This sample doesn't have a ${fillState}`;
    if (showAnnotations) {
      message += ' annotated';
    } else {
      message += ' clean';
    }
    message += ' variant. Showing available variant instead.';

    warningEl.textContent = message;
    warningEl.style.display = 'block';
  }

  hideVariantWarning() {
    document.getElementById('variant-warning').style.display = 'none';
  }

  showLoading() {
    document.getElementById('loading-indicator').style.display = 'block';
    document.querySelector('.image-wrapper').style.display = 'none';
  }

  hideLoading() {
    document.getElementById('loading-indicator').style.display = 'none';
  }

  showError(message) {
    const errorEl = document.getElementById('error-indicator');
    errorEl.textContent = message;
    errorEl.style.display = 'block';
    document.querySelector('.image-wrapper').style.display = 'none';
  }

  hideError() {
    document.getElementById('error-indicator').style.display = 'none';
  }
}

// Initialize viewer when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('dataset-viewer')) {
    new DatasetViewer('dataset-viewer');
  }
});
