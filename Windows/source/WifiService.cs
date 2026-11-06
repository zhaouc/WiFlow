using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security;
using System.Text;

namespace CampusConnect;

// Only fields required for choosing the correct wireless connection.
// Auth is DOT11_AUTH_ALGORITHM, needed to distinguish personal and enterprise Wi-Fi.
// SavedProfileName comes from Windows WLAN AutoConfig, not from our app.
internal sealed record WifiNetwork(Guid InterfaceId, string Ssid, uint Signal, bool Secure, bool Connectable, uint Auth, string SavedProfileName);
internal sealed record WifiSnapshot(string? Current, IReadOnlyList<WifiNetwork> Networks);

// Native WLAN avoids parsing localized netsh output and keeps passwords out of command lines.
internal sealed class WifiService : IDisposable
{
    private IntPtr handle;
    public WifiService() => Check(WlanOpenHandle(2, IntPtr.Zero, out _, out handle));
    public void Dispose() { if (handle != IntPtr.Zero) { WlanCloseHandle(handle, IntPtr.Zero); handle = IntPtr.Zero; } }

    // Opening a WLAN handle and even querying the current connection can wait
    // on a driver/service. Keep the entire handle lifetime off the UI thread.
    public static Task<string?> ReadCurrentAsync(string? preferred = null) => Task.Run(() => {
        using var wifi = new WifiService();
        return wifi.CurrentSsid(preferred);
    });
    public static Task<Guid?> ReadInterfaceAsync(string ssid) => Task.Run(() => {
        using var wifi = new WifiService();
        var connection = wifi.CurrentConnection(ssid);
        return connection.Ssid == ssid ? connection.InterfaceId : null;
    });
    public static Task<WifiSnapshot> ReadSnapshotAsync(string? preferred = null, bool scan = true, bool waitForScan = true) => Task.Run(async () => {
        using var wifi = new WifiService();
        return await wifi.ScanAsync(preferred, scan, waitForScan).ConfigureAwait(false);
    });
    public static Task JoinAsync(CampusProfile profile, string wifiPassword, WifiSnapshot snapshot, bool allowSwitch = false) => Task.Run(async () => {
        using var wifi = new WifiService();
        await wifi.ConnectAsync(profile, wifiPassword, snapshot, allowSwitch).ConfigureAwait(false);
    });

    private List<InterfaceInfo> Interfaces()
    {
        Check(WlanEnumInterfaces(handle, IntPtr.Zero, out var pointer));
        try { return ReadList<InterfaceInfo>(pointer); }
        finally { WlanFreeMemory(pointer); }
    }
    private static List<T> ReadList<T>(IntPtr pointer) where T : struct
    {
        var count = Marshal.ReadInt32(pointer);
        if (count < 0 || count > 10000) throw new InvalidDataException("无线接口返回了无效数据。");
        var result = new List<T>();
        for (var i = 0; i < count; i++) result.Add(Marshal.PtrToStructure<T>(IntPtr.Add(pointer, 8 + i * Marshal.SizeOf<T>())));
        return result;
    }
    public string? CurrentSsid(string? preferred = null) => CurrentConnection(preferred).Ssid;
    private (string? Ssid, Guid? InterfaceId) CurrentConnection(string? preferred)
    {
        (string? Ssid, Guid? InterfaceId) first = (null, null);
        foreach (var item in Interfaces()) {
            var id = item.Id;
            var code = WlanQueryInterface(handle, ref id, 7, IntPtr.Zero, out _, out var data, out _);
            if (code == 5023) continue; // ERROR_INVALID_STATE: interface is disconnected.
            Check(code);
            try {
                var connection = Marshal.PtrToStructure<ConnectionAttributes>(data);
                if (connection.State != 1) continue;
                var ssid = connection.Association.Ssid.Text;
                if (ssid == preferred) return (ssid, id);
                if (first.Ssid is null) first = (ssid, id);
            } finally { WlanFreeMemory(data); }
        }
        return first;
    }
    public async Task<WifiSnapshot> ScanAsync(string? preferred = null, bool scan = true, bool waitForScan = true)
    {
        var interfaces = Interfaces();
        if (interfaces.Count == 0) throw new InvalidOperationException("没有找到 Wi-Fi 网卡，请检查无线网卡和 WLAN AutoConfig 服务。");
        if (scan && waitForScan) {
            RequestScan(interfaces);
            await Task.Delay(1800).ConfigureAwait(false);
        }
        var networks = new List<WifiNetwork>();
        foreach (var item in interfaces) {
            var id = item.Id;
            var code = WlanGetAvailableNetworkList(handle, ref id, 0, IntPtr.Zero, out var pointer);
            if (code == 5023 || code == 2150899714) continue; // Radio powered off.
            Check(code);
            try {
                foreach (var n in ReadList<AvailableNetwork>(pointer))
                    if (n.Ssid.Length > 0) networks.Add(new(id, n.Ssid.Text, n.Signal, n.SecurityEnabled != 0, n.Connectable != 0, n.Auth, n.ProfileName ?? ""));
            } finally { WlanFreeMemory(pointer); }
        }
        var snapshot = new WifiSnapshot(CurrentSsid(preferred), networks.OrderByDescending(n => n.Signal).ToList());
        // WlanScan may flush its cache: read the previous result BEFORE requesting
        // a background scan. The next scheduled check reads the refreshed result.
        if (scan && !waitForScan) RequestScan(interfaces);
        return snapshot;
    }
    private void RequestScan(IEnumerable<InterfaceInfo> interfaces)
    {
        foreach (var item in interfaces) {
            var id = item.Id;
            var code = WlanScan(handle, ref id, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
            if (code == 5) Check(code);
        }
    }
    public async Task ConnectAsync(CampusProfile profile, string wifiPassword, WifiSnapshot snapshot, bool allowSwitch = false)
    {
        var current = CurrentSsid(profile.Ssid);
        if (current == profile.Ssid) return;
        if (!allowSwitch && !string.IsNullOrEmpty(current))
            throw new InvalidOperationException("当前连接其他网络，自动连接已暂停。");

        // A network's SSID is not its Windows profile name. Keep the user's
        // existing Windows profile, even if the UI contains a Wi-Fi password.
        // Old app-generated profiles are never reused; launch-time cleanup
        // removes verified legacy profiles, including those that were active.
        var nearby = snapshot.Networks.Where(n => n.Ssid == profile.Ssid && n.Connectable).ToList();
        if (nearby.Count == 0)
            throw new InvalidOperationException("附近找不到指定的 Wi-Fi，或无线网卡无法连接该网络。");

        // Prefer the system's original SSID profile on the matching adapter.
        // The available-network list can advertise a different (legacy) profile
        // even when a proper SSID-named profile is saved, hence profile-list lookup.
        WifiNetwork? network = null;
        string? savedProfile = null;
        foreach (var candidate in nearby.OrderByDescending(n => n.Secure)) {
            var profiles = SavedProfiles(candidate.InterfaceId);
            var name = profiles.Contains(profile.Ssid) ? profile.Ssid :
                IsOriginalSavedProfile(candidate.SavedProfileName) && profiles.Contains(candidate.SavedProfileName)
                    ? candidate.SavedProfileName : null;
            if (name is null) continue;
            network = candidate;
            savedProfile = name;
            break;
        }

        if (network is null) {
            // An unsaved, password-protected network needs a new permanent
            // Windows profile. 802.1X/WEP/OWE must be configured in Windows.
            network = nearby.FirstOrDefault(n => n.Secure && n.Auth is 7 or 9)
                ?? nearby.FirstOrDefault(n => !n.Secure && n.Auth == 1);
            if (network is null)
                throw new InvalidOperationException("此 Wi-Fi 使用企业级认证或不支持的加密方式，请先在 Windows 网络设置中配置。");
            if (network.Secure) {
                if (string.IsNullOrEmpty(wifiPassword))
                    throw new InvalidOperationException("Windows 未保存此 Wi-Fi，请输入密码以创建系统永久连接配置。");
                if (wifiPassword.Length < 8 || wifiPassword.Length > 63)
                    throw new InvalidOperationException("WPA2/WPA3 个人 Wi-Fi 密码应为 8 至 63 个字符。");
            }
        }

        var id = network.InterfaceId;
        // Revalidate before changing any saved configuration, since scanning
        // and retrieving profiles can take time while another app connects.
        current = CurrentSsid(profile.Ssid);
        if (current == profile.Ssid) return;
        if (!allowSwitch && !string.IsNullOrEmpty(current))
            throw new InvalidOperationException("网络已变化，自动连接已暂停。");

        ConnectionParameters parameters;
        if (savedProfile is not null) {
            // Never write, rename, or overwrite any existing Windows profile.
            parameters = new ConnectionParameters { Mode = 0, Profile = savedProfile, BssType = 1 };
        } else if (network.Secure) {
            // Create an all-user Windows WLAN profile named EXACTLY the SSID.
            // bOverwrite=false prevents accidental replacement of existing
            // passwords or configurations. No netsh/command-line secrets.
            var xml = PersonalProfileXml(profile.Ssid, wifiPassword, network.Auth == 9);
            var code = WlanSetProfile(handle, ref id, 0, xml, IntPtr.Zero, false, IntPtr.Zero, out var reason);
            if (code == 5) {
                // Restricted machines may permit a per-user rather than an
                // all-user saved profile. Both are permanent Windows profiles.
                code = WlanSetProfile(handle, ref id, 2, xml, IntPtr.Zero, false, IntPtr.Zero, out reason);
            }
            if (code == 183) { // ERROR_ALREADY_EXISTS; never overwrite a Windows profile
                if (!SavedProfiles(id).Contains(profile.Ssid))
                    throw new InvalidOperationException("Windows 已有同一无线网络的其他配置，无法创建原始 SSID 配置。请在 Windows 网络设置中检查旧配置；程序不会覆盖或删除它。");
            } else if (code == 5) {
                throw new InvalidOperationException("Windows 不允许保存 Wi-Fi 配置；请检查当前账户的 WLAN 配置写入权限。");
            } else if (code == 1206) { // ERROR_BAD_PROFILE
                throw new InvalidOperationException($"Windows 拒绝保存 Wi-Fi 配置，原因代码：{reason}。");
            } else {
                Check(code);
            }
            parameters = new ConnectionParameters { Mode = 0, Profile = profile.Ssid, BssType = 1 };
        } else {
            // An unsaved open network has no password to persist. Use a
            // temporary direct connection without altering the profile store.
            parameters = new ConnectionParameters { Mode = 1, Profile = OpenProfileXml(profile.Ssid), BssType = 1 };
        }

        Check(WlanConnect(handle, ref id, ref parameters, IntPtr.Zero));
        for (var i = 0; i < 25; i++) {
            await Task.Delay(1000).ConfigureAwait(false);
            if (CurrentSsid(profile.Ssid) == profile.Ssid) return;
        }
        throw new TimeoutException("连接超时，请核对 Wi-Fi 密码和信号；新创建的系统配置会保留，可在 Windows 设置中管理。");
    }

    private HashSet<string> SavedProfiles(Guid interfaceId)
    {
        Check(WlanGetProfileList(handle, ref interfaceId, IntPtr.Zero, out var pointer));
        try {
            return ReadList<SavedProfileInfo>(pointer)
                .Select(p => p.Name)
                .Where(n => !string.IsNullOrWhiteSpace(n))
                .ToHashSet(StringComparer.Ordinal);
        } finally { WlanFreeMemory(pointer); }
    }

    private static bool IsOriginalSavedProfile(string? profileName) =>
        !string.IsNullOrWhiteSpace(profileName) &&
        !IsLegacyProfileName(profileName);

    // Old releases used "WIFI助手-" + the first 16 uppercase SHA256 hex digits
    // of the UTF-8 SSID as a Windows profile NAME. The migration verifies both
    // the old naming rule and the SSID inside WLAN profile XML before deletion.
    // Remove even the active old profile: requested one-time direct cleanup can
    // interrupt Wi-Fi and requires new credentials when no original exists.
    private const string LegacyProfilePrefix = "WIFI助手-";
    private static bool IsLegacyProfileName(string? name) =>
        name is not null && name.Length == LegacyProfilePrefix.Length + 16 &&
        name.StartsWith(LegacyProfilePrefix, StringComparison.Ordinal) &&
        name.AsSpan(LegacyProfilePrefix.Length).ToString().All(c => c is >= '0' and <= '9' or >= 'A' and <= 'F');

    public static Task<(int Removed, int Skipped)> CleanupLegacyProfilesAsync() => Task.Run(() => {
        using var wifi = new WifiService();
        return wifi.CleanupLegacyProfiles();
    });

    private (int Removed, int Skipped) CleanupLegacyProfiles()
    {
        int removed = 0, skipped = 0;
        foreach (var item in Interfaces()) {
            var id = item.Id;
            HashSet<string> profiles;
            try { profiles = SavedProfiles(id); }
            catch { skipped++; continue; }
            foreach (var name in profiles.Where(IsLegacyProfileName)) {
                try {
                    // Only delete the precise profile name generated by the old
                    // program AND matching its embedded SSID. Do not touch any
                    // normal Windows profiles, even those with similar names.
                    if (!IsVerifiedLegacyProfile(id, name)) { skipped++; continue; }
                    var code = WlanDeleteProfile(handle, ref id, name, IntPtr.Zero);
                    if (code == 0) removed++;
                    else skipped++;
                } catch { skipped++; }
            }
        }
        return (removed, skipped);
    }

    private bool IsVerifiedLegacyProfile(Guid interfaceId, string profileName)
    {
        var id = interfaceId;
        var code = WlanGetProfile(handle, ref id, profileName, IntPtr.Zero,
                                  out var pointer, out _, out _);
        if (code != 0) return false;
        try {
            var xml = Marshal.PtrToStringUni(pointer);
            if (string.IsNullOrWhiteSpace(xml)) return false;
            var root = System.Xml.Linq.XDocument.Parse(xml).Root;
            if (root is null) return false;
            var ns = root.Name.Namespace;
            if ((string?)root.Element(ns + "name") != profileName) return false;
            var ssid = root.Element(ns + "SSIDConfig")?.Element(ns + "SSID");
            var hex = (string?)ssid?.Element(ns + "hex");
            if (string.IsNullOrWhiteSpace(hex)) return false;
            var bytes = Convert.FromHexString(hex);
            // Validate the SSID and the exact original naming algorithm.
            if (bytes.Length == 0 || bytes.Length > 32) return false;
            var ssidName = (string?)ssid?.Element(ns + "name");
            if (!string.IsNullOrEmpty(ssidName) &&
                !string.Equals(Encoding.UTF8.GetString(bytes), ssidName, StringComparison.Ordinal)) return false;
            var expected = LegacyProfilePrefix +
                Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(bytes))[..16];
            return string.Equals(expected, profileName, StringComparison.Ordinal);
        } finally { WlanFreeMemory(pointer); }
    }

    internal static string OpenProfileXml(string ssid)
    {
        var hex = Convert.ToHexString(Encoding.UTF8.GetBytes(ssid));
        return $"""
            <?xml version="1.0"?>
            <WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
              <name>{SecurityElement.Escape(ssid)}</name>
              <SSIDConfig><SSID><hex>{hex}</hex><name>{SecurityElement.Escape(ssid)}</name></SSID></SSIDConfig>
              <connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
              <MSM><security><authEncryption><authentication>open</authentication><encryption>none</encryption><useOneX>false</useOneX></authEncryption></security></MSM>
            </WLANProfile>
            """;
    }
    internal static string PersonalProfileXml(string ssid, string password, bool wpa3 = false)
    {
        var hex = Convert.ToHexString(Encoding.UTF8.GetBytes(ssid));
        return $"""
            <?xml version="1.0"?>
            <WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
              <name>{SecurityElement.Escape(ssid)}</name>
              <SSIDConfig><SSID><hex>{hex}</hex><name>{SecurityElement.Escape(ssid)}</name></SSID></SSIDConfig>
              <connectionType>ESS</connectionType><connectionMode>auto</connectionMode>
              <MSM><security><authEncryption><authentication>{(wpa3 ? "WPA3SAE" : "WPA2PSK")}</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
                <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{SecurityElement.Escape(password)}</keyMaterial></sharedKey>
              </security></MSM>
            </WLANProfile>
            """;
    }
    private static void Check(uint code)
    {
        if (code == 5) throw new InvalidOperationException("Windows 拒绝访问 Wi-Fi 信息。请在 设置 → 隐私和安全性 → 位置 中开启定位服务并允许桌面应用访问，然后重试。");
        if (code != 0) throw new Win32Exception((int)code);
    }

    [StructLayout(LayoutKind.Sequential)] internal struct Ssid
    {
        public uint Length;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 32)] public byte[] Bytes;
        public readonly string Text => Encoding.UTF8.GetString(Bytes, 0, (int)Math.Min(Length, 32));
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] internal struct InterfaceInfo
    {
        public Guid Id;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Description;
        public uint State;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] internal struct AvailableNetwork
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string ProfileName;
        public Ssid Ssid;
        public uint BssType, BssidCount;
        public int Connectable;
        public uint Reason, PhyCount;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 8)] public uint[] Phys;
        public int MorePhys;
        public uint Signal;
        public int SecurityEnabled;
        public uint Auth, Cipher, Flags, Reserved;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] private struct SavedProfileInfo
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Name;
        public uint Flags;
    }
    [StructLayout(LayoutKind.Sequential)] internal struct Association
    {
        public Ssid Ssid;
        public uint BssType;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 6)] public byte[] Bssid;
        public uint PhyType, PhyIndex, Signal, RxRate, TxRate;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] internal struct ConnectionAttributes
    {
        public uint State, Mode;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string ProfileName;
        public Association Association;
        public int SecurityEnabled, OneXEnabled;
        public uint Auth, Cipher;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] private struct ConnectionParameters
    {
        public uint Mode;
        [MarshalAs(UnmanagedType.LPWStr)] public string Profile;
        public IntPtr Ssid, BssidList;
        public uint BssType, Flags;
    }
    [DllImport("wlanapi.dll")] private static extern uint WlanOpenHandle(uint version, IntPtr reserved, out uint negotiated, out IntPtr handle);
    [DllImport("wlanapi.dll")] private static extern uint WlanCloseHandle(IntPtr handle, IntPtr reserved);
    [DllImport("wlanapi.dll")] private static extern uint WlanEnumInterfaces(IntPtr handle, IntPtr reserved, out IntPtr list);
    [DllImport("wlanapi.dll")] private static extern void WlanFreeMemory(IntPtr memory);
    [DllImport("wlanapi.dll")] private static extern uint WlanQueryInterface(IntPtr handle, ref Guid id, uint opcode, IntPtr reserved, out uint size, out IntPtr data, out uint type);
    [DllImport("wlanapi.dll")] private static extern uint WlanScan(IntPtr handle, ref Guid id, IntPtr ssid, IntPtr ie, IntPtr reserved);
    [DllImport("wlanapi.dll")] private static extern uint WlanGetAvailableNetworkList(IntPtr handle, ref Guid id, uint flags, IntPtr reserved, out IntPtr list);
    [DllImport("wlanapi.dll")] private static extern uint WlanGetProfileList(IntPtr handle, ref Guid id, IntPtr reserved, out IntPtr list);
    [DllImport("wlanapi.dll", CharSet = CharSet.Unicode)] private static extern uint WlanGetProfile(
        IntPtr handle, ref Guid id, string name, IntPtr reserved, out IntPtr xml,
        out uint flags, out uint grantedAccess);
    [DllImport("wlanapi.dll", CharSet = CharSet.Unicode)] private static extern uint WlanDeleteProfile(
        IntPtr handle, ref Guid id, string name, IntPtr reserved);
    [DllImport("wlanapi.dll", CharSet = CharSet.Unicode)] private static extern uint WlanSetProfile(IntPtr handle, ref Guid id, uint flags, string xml, IntPtr security, [MarshalAs(UnmanagedType.Bool)] bool overwrite, IntPtr reserved, out uint reason);
    [DllImport("wlanapi.dll", CharSet = CharSet.Unicode)] private static extern uint WlanConnect(IntPtr handle, ref Guid id, ref ConnectionParameters parameters, IntPtr reserved);
}
