using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace CampusConnect;

internal static class LocalStore
{
    public static string DirectoryPath => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CampusConnect");
    private static ConnectionConfiguration? Read(string directory) => File.Exists(Path.Combine(directory, "configuration.json"))
        ? JsonSerializer.Deserialize<ConnectionConfiguration>(File.ReadAllText(Path.Combine(directory, "configuration.json")))
            ?? throw new InvalidDataException("配置文件为空。") : null;
    private static string Unprotect(string value) => string.IsNullOrEmpty(value) ? "" : Encoding.UTF8.GetString(
        ProtectedData.Unprotect(Convert.FromBase64String(value), null, DataProtectionScope.CurrentUser));

    public static (CampusProfile Profile, string Password, string WifiPassword) Load() => LoadFrom(DirectoryPath);
    internal static (CampusProfile Profile, string Password, string WifiPassword) LoadFrom(string directory)
    {
        var config = Read(directory);
        if (config is null) return (ConnectionConfiguration.Empty(false), "", "");
        config.Profile.Validate();
        return (config.Profile, Unprotect(config.ProtectedPassword), Unprotect(config.ProtectedWifiPassword));
    }
    public static Dictionary<string, UserConnection> LoadProfiles() => LoadProfilesFrom(DirectoryPath);
    internal static Dictionary<string, UserConnection> LoadProfilesFrom(string directory) => Read(directory)?.AllProfiles()
        .ToDictionary(entry => entry.Key, entry => new UserConnection(entry.Value.Profile, Unprotect(entry.Value.ProtectedPassword), Unprotect(entry.Value.ProtectedWifiPassword))) ?? [];

    public static void Save(CampusProfile profile, string password, string wifiPassword) => SaveTo(DirectoryPath, profile, password, wifiPassword);
    internal static void SaveTo(string directory, CampusProfile profile, string password, string wifiPassword)
    {
        profile.Validate();
        var protectedPassword = string.IsNullOrEmpty(password) ? "" : Convert.ToBase64String(
            ProtectedData.Protect(Encoding.UTF8.GetBytes(password), null, DataProtectionScope.CurrentUser));
        var protectedWifiPassword = string.IsNullOrEmpty(wifiPassword) ? "" : Convert.ToBase64String(
            ProtectedData.Protect(Encoding.UTF8.GetBytes(wifiPassword), null, DataProtectionScope.CurrentUser));
        var config = (Read(directory) ?? new(profile, protectedPassword)).WithActive(profile, protectedPassword, protectedWifiPassword);
        Write(directory, config);
    }
    public static void UpdateAutomatic(string mode, CampusProfile updated)
    {
        if (mode is not ("wifi" or "wired") || ConnectionConfiguration.Key(updated) != mode)
            throw new ArgumentException("无效的网络类型。", nameof(mode));
        updated.Validate();
        var config = Read(DirectoryPath) ?? throw new InvalidOperationException("请先保存网络配置。");
        var all = config.AllProfiles();
        if (!all.TryGetValue(mode, out var entry)) throw new InvalidOperationException("请先保存网络配置。");
        all[mode] = entry with { Profile = updated };
        config = config with {
            Profiles = all,
            Profile = ConnectionConfiguration.Key(config.Profile) == mode ? updated : config.Profile
        };
        Write(DirectoryPath, config);
    }
    private static void Write(string directory, ConnectionConfiguration config)
    {
        Directory.CreateDirectory(directory);
        var temporary = Path.Combine(directory, $"configuration-{Guid.NewGuid():N}.tmp");
        try {
            File.WriteAllText(temporary, JsonSerializer.Serialize(config, new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temporary, Path.Combine(directory, "configuration.json"), true);
        } finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}
