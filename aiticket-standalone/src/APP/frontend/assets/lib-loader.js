(function(global) {
    const LIBRARY_PATHS = {
        marked: '/assets/vendor/marked.min.js',
        sortable: '/assets/vendor/sortable.min.js',
        chart: '/assets/vendor/chart.min.js',
        lucide: '/assets/vendor/lucide.min.js',
        html2pdf: '/assets/vendor/html2pdf.bundle.min.js',
        mermaid: '/assets/vendor/mermaid.min.js',
        tailwind: '/assets/vendor/tailwind-browser.min.js',
    };

    const inFlightLoads = new Map();

    function loadLibrary(name) {
        const src = LIBRARY_PATHS[name];
        if (!src) {
            return Promise.reject(new Error(`Unknown library: ${name}`));
        }

        if (inFlightLoads.has(name)) {
            return inFlightLoads.get(name);
        }

        const existing = document.querySelector(`script[data-lib="${name}"]`);
        if (existing) {
            const ready = existing.dataset.loaded === 'true'
                ? Promise.resolve(existing)
                : new Promise((resolve, reject) => {
                    existing.addEventListener('load', () => resolve(existing), { once: true });
                    existing.addEventListener('error', () => reject(new Error(`Failed to load ${name}`)), { once: true });
                });
            inFlightLoads.set(name, ready);
            return ready;
        }

        const promise = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = src;
            script.async = true;
            script.dataset.lib = name;
            script.onload = () => {
                script.dataset.loaded = 'true';
                resolve(script);
            };
            script.onerror = () => {
                inFlightLoads.delete(name);
                script.remove();
                reject(new Error(`Failed to load ${name}`));
            };
            document.head.appendChild(script);
        });

        inFlightLoads.set(name, promise);
        return promise;
    }

    function loadLibraries(names) {
        return Promise.all(names.map((name) => loadLibrary(name)));
    }

    global.FrontendLibLoader = {
        loadLibrary,
        loadLibraries,
        LIBRARY_PATHS,
    };
    global.loadLibrary = loadLibrary;
    global.loadLibraries = loadLibraries;
})(typeof window !== 'undefined' ? window : globalThis);
