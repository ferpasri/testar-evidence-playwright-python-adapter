(options) => {
    const nodes = [];
    const excluded = new Set(options.excludedAttributes);
    const liveNames = ["value", "checked", "selected", "disabled", "readOnly", "required", "selectedIndex", "currentSrc", "src", "href"];
    let retained = 0;
    let truncated = false;
    let omittedFrames = 0;
    let omissionsNotListed = 0;

    function propertiesFor(element) {
        const properties = {};
        const aliases = {};
        const omissions = [];
        let omittedCount = 0;
        const secret = element instanceof HTMLInputElement && element.type === "password";

        function omit(property, reason) {
            if (reason === "limit") {
                truncated = true;
            }
            omittedCount += 1;
            if (omissions.length < 32 && property.length <= 512) {
                omissions.push({ property, reason });
            } else {
                omissionsNotListed += 1;
            }
        }

        function save(property, value) {
            if (typeof value === "string" && value.length > options.maxValueCharacters) {
                omit(property, "limit");
                return;
            }
            const size = property.length + JSON.stringify(value).length;
            if (retained + size > options.maxTotalCharacters) {
                omit(property, "limit");
                return;
            }
            properties[property] = value;
            retained += size;
        }

        function alias(name, source) {
            if (Object.hasOwn(properties, source)) {
                save(name, properties[source]);
                if (Object.hasOwn(properties, name)) {
                    aliases[name] = source;
                }
            }
        }

        save("playwright.dom.tagName", element.tagName);
        let attributes = 0;
        for (const attribute of element.attributes) {
            const name = "playwright.attribute." + attribute.name;
            if (secret && attribute.name === "value") {
                omit(name, "redacted");
            } else if (excluded.has(attribute.name)) {
                omit(name, "excluded");
            } else if (attributes >= options.maxAttributesPerWidget) {
                omit(name, "limit");
            } else {
                attributes += 1;
                save(name, attribute.value);
            }
        }
        save("playwright.dom.textContent", element.textContent);
        alias("ui.text", "playwright.dom.textContent");
        alias("ui.alt", "playwright.attribute.alt");
        alias("ui.role", "playwright.attribute.role");
        for (const name of liveNames) {
            if (!(name in element)) {
                continue;
            }
            const property = "playwright.dom." + name;
            if (secret && name === "value") {
                omit(property, "redacted");
                continue;
            }
            try {
                const value = element[name];
                if (value === null || typeof value === "string" || typeof value === "boolean"
                    || (typeof value === "number" && Number.isFinite(value))) {
                    save(property, value);
                } else {
                    omit(property, "unsupported");
                }
            } catch {
                omit(property, "unavailable");
            }
        }
        alias("ui.value", "playwright.dom.value");
        alias("ui.checked", "playwright.dom.checked");
        alias("ui.selected", "playwright.dom.selected");
        const bounds = element.getBoundingClientRect();
        save("playwright.dom.bounds", { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height });
        alias("ui.bounds", "playwright.dom.bounds");
        if (options.computedStyles.length > 0) {
            try {
                const styles = getComputedStyle(element);
                for (const name of options.computedStyles) {
                    save("playwright.style." + name, styles.getPropertyValue(name));
                }
            } catch {
                for (const name of options.computedStyles) {
                    omit("playwright.style." + name, "unavailable");
                }
            }
        }
        if (Object.keys(aliases).length > 0) {
            properties["evidence.property_aliases"] = aliases;
        }
        if (omissions.length > 0) {
            properties["evidence.omitted_properties"] = omissions;
        }
        if (omittedCount > omissions.length) {
            properties["playwright.capture.unlisted_omissions"] = omittedCount - omissions.length;
        }
        return properties;
    }

    nodes.push({ path: "#document", parent: null, properties: { "playwright.dom.nodeType": document.nodeType } });
    const pending = document.documentElement
        ? [{ element: document.documentElement, parent: "#document", path: "#document/html[0]" }] : [];
    while (pending.length > 0) {
        if (nodes.length >= options.maxWidgets) {
            truncated = true;
            break;
        }
        const current = pending.pop();
        nodes.push({ path: current.path, parent: current.parent, properties: propertiesFor(current.element) });
        if (current.element.matches("iframe, frame")) {
            omittedFrames += 1;
        }
        const children = Array.from(current.element.children).map((element, index) => ({
            element, parent: current.path, path: current.path + "/" + element.tagName.toLowerCase() + "[" + index + "]"
        }));
        if (current.element.shadowRoot) {
            Array.from(current.element.shadowRoot.children).forEach((element, index) => children.push({
                element, parent: current.path, path: current.path + "/#shadow-root/" + element.tagName.toLowerCase() + "[" + index + "]"
            }));
        }
        for (let index = children.length - 1; index >= 0; index -= 1) {
            pending.push(children[index]);
        }
    }
    return {
        nodes,
        properties: {
            "playwright.document.url": document.URL,
            "playwright.document.title": document.title,
            "ui.url": document.URL,
            "ui.title": document.title,
            "evidence.property_aliases": { "ui.url": "playwright.document.url", "ui.title": "playwright.document.title" },
            "evidence.capture": {
                area: "Top document and open shadow roots; frame documents are excluded and closed shadow roots are not observable",
                widget_selection: "Document and DOM elements in traversal order",
                property_selection: "DOM attributes except configured exclusions; raw textContent; live DOM values; bounds; configured computed styles. Password values are redacted.",
                limits: {
                    widget_count: options.maxWidgets,
                    widget_value_characters: options.maxValueCharacters,
                    total_widget_property_characters: options.maxTotalCharacters,
                    attributes_per_widget: options.maxAttributesPerWidget,
                    omission_entries_per_widget: 32
                },
                truncated
            },
            "playwright.capture.computed_styles": options.computedStyles,
            "playwright.capture.excluded_attributes": options.excludedAttributes,
            "playwright.capture.omitted_frame_documents": omittedFrames,
            "playwright.capture.property_characters_retained": retained,
            "playwright.capture.unlisted_omissions": omissionsNotListed
        }
    };
}
