<?php
// Docker development only. Read installed plugin/theme headers, not WP options,
// users, keys or database data. No WordPress bootstrap and no executable actions.
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
$version_file = '/var/www/html/wp-includes/version.php';
if (!is_file($version_file)) {
    http_response_code(503);
    echo json_encode(['error' => 'WordPress files are not installed']);
    exit;
}
require $version_file;

/** Read bounded header text without executing plugin or theme PHP. */
function header_value(string $path, string $key): string {
    if (!is_file($path) || is_link($path)) {
        return '';
    }
    $text = file_get_contents($path, false, null, 0, 8192);
    if ($text === false) {
        return '';
    }
    $pattern = '/^[ \t\/*#@]*' . preg_quote($key, '/') . ':\s*(.{1,120})\s*$/mi';
    if (!preg_match($pattern, $text, $matches)) {
        return '';
    }
    return trim(strip_tags($matches[1]));
}

function collect_themes(): array {
    $items = [];
    $root = '/var/www/html/wp-content/themes';
    if (!is_dir($root)) {
        return $items;
    }
    foreach (new DirectoryIterator($root) as $entry) {
        if (count($items) >= 40) { break; }
        if (!$entry->isDir() || $entry->isLink() || $entry->isDot()) { continue; }
        $path = $entry->getPathname() . '/style.css';
        $name = header_value($path, 'Theme Name');
        if ($name !== '') {
            $items[] = ['name' => $name, 'version' => header_value($path, 'Version')];
        }
    }
    usort($items, fn ($a, $b) => strcmp($a['name'], $b['name']));
    return $items;
}

function collect_plugins(): array {
    $items = [];
    $root = '/var/www/html/wp-content/plugins';
    if (!is_dir($root)) {
        return $items;
    }
    foreach (new DirectoryIterator($root) as $entry) {
        if (count($items) >= 40) { break; }
        if ($entry->isLink() || $entry->isDot()) { continue; }
        $candidates = [];
        if ($entry->isFile() && str_ends_with($entry->getFilename(), '.php')) {
            $candidates[] = $entry->getPathname();
        } elseif ($entry->isDir()) {
            // Only inspect immediate PHP files in a single plugin directory.
            foreach (new DirectoryIterator($entry->getPathname()) as $child) {
                if (count($candidates) >= 30) { break; }
                if ($child->isFile() && !$child->isLink() &&
                    str_ends_with($child->getFilename(), '.php')) {
                    $candidates[] = $child->getPathname();
                }
            }
        }
        foreach ($candidates as $path) {
            $name = header_value($path, 'Plugin Name');
            if ($name !== '') {
                $items[] = ['name' => $name, 'version' => header_value($path, 'Version')];
                break;
            }
        }
    }
    usort($items, fn ($a, $b) => strcmp($a['name'], $b['name']));
    return $items;
}

echo json_encode([
    'php_version' => PHP_VERSION,
    'wordpress_version' => $wp_version,
    'plugins' => collect_plugins(),
    'themes' => collect_themes(),
], JSON_THROW_ON_ERROR);
