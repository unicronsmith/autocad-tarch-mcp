// 在天正 AutoCAD 会话里把每个天正对象在内存中分解一遍，逐个写出它画出来的图元。
//
//     (tchdump "C:/out.jsonl" "C:/deny.txt")   返回写出的对象数
//
// Entity.Explode 不改图纸：分解结果是不进数据库的临时图元，写完就丢。所以不用切空间、不用撤销，
// 模型空间、布局、块定义里的对象一视同仁。天正的「分解对象」命令（TExplode）是弹对话框的整图操作，
// 原生 EXPLODE 命令只认当前空间的顶层对象，都做不到逐个对象取。
//
//     (tchprops "C:/out.jsonl" "C:/deny.txt")   逐个对象读天正 COM 属性（墙高、门窗编号、柱截面……）
//
// 输出一行一个 JSON。每个对象先写一行「#句柄 类名」再动它，tchprops 每读一个属性前再写一行「#类名<TAB>属性名」：
// AutoCAD 当场崩了，最后一行就是肇事的对象或属性（天正的属性读取崩过 acad.exe）。deny.txt 是以前崩过的，跳过：
// 「类名<TAB>属性名」不读这个属性，「类名<TAB>*」这个类的属性全不读，「DXF名<TAB>!explode」这个类不分解。
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
using Autodesk.AutoCAD.ApplicationServices.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;

[assembly: CommandClass(typeof(TchDump.Commands))]

namespace TchDump
{
    [ComImport, Guid("00020400-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IDispatchInfo
    {
        [PreserveSig] int GetTypeInfoCount(out int count);
        [PreserveSig] int GetTypeInfo(int index, int lcid, out ITypeInfo typeInfo);
    }

    public class Commands
    {
        const string Prefix = "TCH_";
        // 天正对象分解出来还是天正对象时接着分解（门窗套在墙里、符号里套文字），到这个深度为止
        const int MaxDepth = 4;

        static List<string> Texts(ResultBuffer args)
        {
            var texts = new List<string>();
            if (args != null)
                foreach (TypedValue tv in args)
                    if (tv.TypeCode == (int)LispDataType.Text)
                        texts.Add((string)tv.Value);
            return texts;
        }

        static HashSet<string> Deny(List<string> texts)
        {
            var deny = new HashSet<string>();
            if (texts.Count > 1 && File.Exists(texts[1]))
                foreach (string line in File.ReadAllLines(texts[1], Encoding.UTF8))
                    if (line.Trim().Length > 0)
                        deny.Add(line.Trim());
            return deny;
        }

        [LispFunction("tchdump")]
        public static object Dump(ResultBuffer args)
        {
            List<string> texts = Texts(args);
            if (texts.Count == 0)
                return null;
            string path = texts[0];
            HashSet<string> deny = Deny(texts);
            Database db = Application.DocumentManager.MdiActiveDocument.Database;
            int count = 0;
            using (var w = new StreamWriter(path, false, new UTF8Encoding(false)))
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                w.AutoFlush = true;
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                foreach (ObjectId btrId in bt)
                {
                    var btr = (BlockTableRecord)tr.GetObject(btrId, OpenMode.ForRead);
                    if (btr.IsFromExternalReference || btr.IsDependent)
                        continue;
                    string layout = null;
                    if (btr.IsLayout)
                        layout = ((Layout)tr.GetObject(btr.LayoutId, OpenMode.ForRead)).LayoutName;
                    foreach (ObjectId id in btr)
                    {
                        string dxf = id.ObjectClass.DxfName ?? "";
                        if (!dxf.StartsWith(Prefix, StringComparison.OrdinalIgnoreCase))
                            continue;
                        w.WriteLine("#" + id.Handle + " " + dxf);
                        var sb = new StringBuilder();
                        sb.Append("{\"h\":").Append(Str(id.Handle.ToString()));
                        sb.Append(",\"dxf\":").Append(Str(dxf));
                        sb.Append(",\"cls\":").Append(Str(id.ObjectClass.Name));
                        sb.Append(",\"owner\":").Append(Str(btr.Name));
                        if (layout != null)
                            sb.Append(",\"layout\":").Append(Str(layout));
                        try
                        {
                            var ent = (Entity)tr.GetObject(id, OpenMode.ForRead);
                            sb.Append(",\"layer\":").Append(Str(ent.Layer));
                            AppendExtents(sb, ent);
                            if (deny.Contains(dxf + "	!explode"))
                                sb.Append(",\"skipped\":true");
                            sb.Append(",\"parts\":[");
                            int n = 0;
                            if (!deny.Contains(dxf + "	!explode"))
                                ExplodeInto(sb, ent, tr, 0, ref n);
                            sb.Append("]");
                        }
                        catch (System.Exception ex)
                        {
                            sb.Append(",\"err\":").Append(Str(ex.GetType().Name + ": " + ex.Message));
                        }
                        sb.Append("}");
                        w.WriteLine(sb.ToString());
                        count++;
                    }
                }
                w.WriteLine("{\"done\":" + count + "}");
                tr.Commit();
            }
            return count;
        }

        // 这些属性返回的是 AutoCAD 对象或跟识别无关，不读
        static readonly HashSet<string> SkipProps = new HashSet<string> {
            "Application", "Document", "Database", "Hyperlinks", "Material", "PlotStyleName", "TrueColor",
            "OwnerID", "ObjectID", "ObjectID32", "OwnerID32", "HasExtensionDictionary", "EntityTransparency" };

        [LispFunction("tchprops")]
        public static object Props(ResultBuffer args)
        {
            List<string> texts = Texts(args);
            if (texts.Count == 0)
                return null;
            HashSet<string> deny = Deny(texts);
            Database db = Application.DocumentManager.MdiActiveDocument.Database;
            var names = new Dictionary<string, List<string>>();
            int count = 0;
            using (var w = new StreamWriter(texts[0], false, new UTF8Encoding(false)))
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                w.AutoFlush = true;
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                foreach (ObjectId btrId in bt)
                {
                    var btr = (BlockTableRecord)tr.GetObject(btrId, OpenMode.ForRead);
                    if (btr.IsFromExternalReference || btr.IsDependent)
                        continue;
                    foreach (ObjectId id in btr)
                    {
                        if (!(id.ObjectClass.DxfName ?? "").StartsWith(Prefix, StringComparison.OrdinalIgnoreCase))
                            continue;
                        string cls = id.ObjectClass.Name;
                        w.WriteLine("#" + id.Handle + " " + cls);
                        var sb = new StringBuilder();
                        sb.Append("{\"h\":").Append(Str(id.Handle.ToString())).Append(",\"cls\":").Append(Str(cls));
                        object com = null;
                        if (deny.Contains(cls + "	*"))
                        {
                            w.WriteLine(sb.Append(",\"skipped\":true,\"props\":{}}").ToString());
                            count++;
                            continue;
                        }
                        try
                        {
                            var ent = (Entity)tr.GetObject(id, OpenMode.ForRead);
                            com = ent.AcadObject;
                            if (!names.ContainsKey(cls))
                                names[cls] = PropertyNames(com);
                            var failed = new List<string>();
                            sb.Append(",\"props\":{");
                            int n = 0;
                            foreach (string name in names[cls])
                            {
                                if (deny.Contains(cls + "\t" + name))
                                    continue;
                                w.WriteLine("#" + cls + "\t" + name);
                                try
                                {
                                    object v = com.GetType().InvokeMember(name, BindingFlags.GetProperty, null, com, null,
                                                                         CultureInfo.InvariantCulture);
                                    if (n++ > 0) sb.Append(",");
                                    sb.Append(Str(name)).Append(":").Append(Val(v));
                                }
                                catch (System.Exception)
                                {
                                    failed.Add(name);
                                }
                            }
                            sb.Append("}");
                            if (failed.Count > 0)
                            {
                                sb.Append(",\"failed\":[");
                                for (int i = 0; i < failed.Count; i++)
                                    sb.Append(i > 0 ? "," : "").Append(Str(failed[i]));
                                sb.Append("]");
                            }
                        }
                        catch (System.Exception ex)
                        {
                            sb.Append(",\"err\":").Append(Str(ex.GetType().Name + ": " + ex.Message));
                        }
                        finally
                        {
                            if (com != null && Marshal.IsComObject(com))
                                Marshal.ReleaseComObject(com);
                        }
                        sb.Append("}");
                        w.WriteLine(sb.ToString());
                        count++;
                    }
                }
                w.WriteLine("{\"done\":" + count + "}");
                tr.Commit();
            }
            return count;
        }

        static List<string> PropertyNames(object com)
        {
            var names = new List<string>();
            var disp = com as IDispatchInfo;
            if (disp == null || disp.GetTypeInfo(0, 0, out ITypeInfo ti) != 0 || ti == null)
                return names;
            ti.GetTypeAttr(out IntPtr pAttr);
            int funcs = Marshal.PtrToStructure<System.Runtime.InteropServices.ComTypes.TYPEATTR>(pAttr).cFuncs;
            ti.ReleaseTypeAttr(pAttr);
            for (int i = 0; i < funcs; i++)
            {
                ti.GetFuncDesc(i, out IntPtr pFunc);
                var fd = Marshal.PtrToStructure<System.Runtime.InteropServices.ComTypes.FUNCDESC>(pFunc);
                if (fd.invkind == System.Runtime.InteropServices.ComTypes.INVOKEKIND.INVOKE_PROPERTYGET && fd.cParams == 0)
                {
                    var n = new string[1];
                    ti.GetNames(fd.memid, n, 1, out int got);
                    if (got > 0 && !SkipProps.Contains(n[0]) && !names.Contains(n[0]))
                        names.Add(n[0]);
                }
                ti.ReleaseFuncDesc(pFunc);
            }
            names.Sort(StringComparer.Ordinal);
            return names;
        }

        static string Val(object v)
        {
            switch (v)
            {
                case null: return "null";
                case string s: return Str(s);
                case bool b: return b ? "true" : "false";
                case double d: return double.IsNaN(d) || double.IsInfinity(d) ? "null" : d.ToString("R", CultureInfo.InvariantCulture);
                case float f: return float.IsNaN(f) || float.IsInfinity(f) ? "null" : f.ToString("R", CultureInfo.InvariantCulture);
                case sbyte _: case byte _: case short _: case ushort _: case int _: case uint _: case long _: case ulong _:
                    return Convert.ToString(v, CultureInfo.InvariantCulture);
                case Array a:
                    var sb = new StringBuilder("[");
                    int i = 0;
                    foreach (object item in a)
                        sb.Append(i++ > 0 ? "," : "").Append(Val(item));
                    return sb.Append("]").ToString();
                default:
                    string kind = "<" + v.GetType().Name + ">";
                    if (Marshal.IsComObject(v)) Marshal.ReleaseComObject(v);
                    return Str(kind);
            }
        }

        static void ExplodeInto(StringBuilder sb, Entity ent, Transaction tr, int depth, ref int n)
        {
            using (var parts = new DBObjectCollection())
            {
                try
                {
                    ent.Explode(parts);
                }
                catch (System.Exception ex)
                {
                    // 分解不了的照原样记一条：类名、图层、包围盒，后面好知道缺了什么
                    if (n++ > 0) sb.Append(",");
                    sb.Append("{\"type\":\"!\",\"dxf\":").Append(Str(ent.GetRXClass().DxfName ?? ""));
                    sb.Append(",\"err\":").Append(Str(ex.GetType().Name + ": " + ex.Message));
                    try { sb.Append(",\"layer\":").Append(Str(ent.Layer)); } catch (System.Exception) { }
                    AppendExtents(sb, ent);
                    sb.Append("}");
                    return;
                }
                foreach (DBObject o in parts)
                {
                    var e = o as Entity;
                    if (e != null)
                    {
                        string dxf = e.GetRXClass().DxfName ?? "";
                        if (dxf.StartsWith(Prefix, StringComparison.OrdinalIgnoreCase) && depth < MaxDepth)
                            ExplodeInto(sb, e, tr, depth + 1, ref n);
                        else
                        {
                            if (n++ > 0) sb.Append(",");
                            Part(sb, e, dxf, tr);
                        }
                    }
                    o.Dispose();
                }
            }
        }

        static void Part(StringBuilder sb, Entity e, string dxf, Transaction tr)
        {
            sb.Append("{\"type\":").Append(Str(dxf));
            try
            {
                sb.Append(",\"layer\":").Append(Str(e.Layer));
                sb.Append(",\"color\":").Append(e.ColorIndex);
                Geometry(sb, e, tr);
            }
            catch (System.Exception ex)
            {
                sb.Append(",\"err\":").Append(Str(ex.GetType().Name + ": " + ex.Message));
            }
            sb.Append("}");
        }

        static void Geometry(StringBuilder sb, Entity e, Transaction tr)
        {
            switch (e)
            {
                case Line l:
                    sb.Append(",\"p1\":").Append(Pt(l.StartPoint)).Append(",\"p2\":").Append(Pt(l.EndPoint));
                    break;
                case Arc a:
                    sb.Append(",\"c\":").Append(Pt(a.Center)).Append(",\"r\":").Append(Num(a.Radius));
                    sb.Append(",\"a1\":").Append(Num(a.StartAngle)).Append(",\"a2\":").Append(Num(a.EndAngle));
                    break;
                case Circle c:
                    sb.Append(",\"c\":").Append(Pt(c.Center)).Append(",\"r\":").Append(Num(c.Radius));
                    break;
                case Polyline pl:
                    sb.Append(",\"closed\":").Append(pl.Closed ? "true" : "false").Append(",\"pts\":[");
                    for (int i = 0; i < pl.NumberOfVertices; i++)
                    {
                        Point2d p = pl.GetPoint2dAt(i);
                        if (i > 0) sb.Append(",");
                        sb.Append("[").Append(Num(p.X)).Append(",").Append(Num(p.Y)).Append(",").Append(Num(pl.GetBulgeAt(i))).Append("]");
                    }
                    sb.Append("]");
                    PolylineWidths(sb, pl);
                    break;
                case Polyline2d p2:
                    sb.Append(",\"closed\":").Append(p2.Closed ? "true" : "false").Append(",\"pts\":[");
                    {
                        int i = 0;
                        foreach (object o in p2)
                        {
                            Vertex2d v = o as Vertex2d;
                            if (v == null && o is ObjectId vid)
                                v = tr.GetObject(vid, OpenMode.ForRead) as Vertex2d;
                            if (v == null) continue;
                            if (i++ > 0) sb.Append(",");
                            sb.Append("[").Append(Num(v.Position.X)).Append(",").Append(Num(v.Position.Y)).Append(",").Append(Num(v.Bulge)).Append("]");
                        }
                    }
                    sb.Append("]");
                    break;
                case DBText t:
                    sb.Append(",\"text\":").Append(Str(t.TextString));
                    sb.Append(",\"pos\":").Append(Pt(t.Position)).Append(",\"align\":").Append(Pt(t.AlignmentPoint));
                    sb.Append(",\"h\":").Append(Num(t.Height)).Append(",\"rot\":").Append(Num(t.Rotation));
                    sb.Append(",\"wf\":").Append(Num(t.WidthFactor));
                    sb.Append(",\"hm\":").Append((int)t.HorizontalMode).Append(",\"vm\":").Append((int)t.VerticalMode);
                    sb.Append(",\"style\":").Append(Str(SymbolName(tr, t.TextStyleId)));
                    break;
                case MText m:
                    sb.Append(",\"text\":").Append(Str(m.Text)).Append(",\"raw\":").Append(Str(m.Contents));
                    sb.Append(",\"pos\":").Append(Pt(m.Location));
                    sb.Append(",\"h\":").Append(Num(m.TextHeight)).Append(",\"rot\":").Append(Num(m.Rotation));
                    sb.Append(",\"w\":").Append(Num(m.Width)).Append(",\"attach\":").Append((int)m.Attachment);
                    sb.Append(",\"style\":").Append(Str(SymbolName(tr, m.TextStyleId)));
                    break;
                case Dimension d:
                    DimensionPart(sb, d);
                    break;
                case BlockReference br:
                    sb.Append(",\"name\":").Append(Str(SymbolName(tr, br.BlockTableRecord)));
                    sb.Append(",\"pos\":").Append(Pt(br.Position)).Append(",\"rot\":").Append(Num(br.Rotation));
                    sb.Append(",\"scale\":[").Append(Num(br.ScaleFactors.X)).Append(",").Append(Num(br.ScaleFactors.Y)).Append("]");
                    Attributes(sb, br, tr);
                    break;
                case Hatch h:
                    HatchPart(sb, h);
                    break;
                case Solid s:
                    sb.Append(",\"pts\":[");
                    for (short i = 0; i < 4; i++)
                    {
                        if (i > 0) sb.Append(",");
                        sb.Append(Pt(s.GetPointAt(i)));
                    }
                    sb.Append("]");
                    break;
                case Ellipse el:
                    sb.Append(",\"c\":").Append(Pt(el.Center));
                    sb.Append(",\"major\":[").Append(Num(el.MajorAxis.X)).Append(",").Append(Num(el.MajorAxis.Y)).Append("]");
                    sb.Append(",\"ratio\":").Append(Num(el.RadiusRatio));
                    sb.Append(",\"a1\":").Append(Num(el.StartAngle)).Append(",\"a2\":").Append(Num(el.EndAngle));
                    break;
                case Spline sp:
                    sb.Append(",\"pts\":[");
                    for (int i = 0; i < sp.NumControlPoints; i++)
                    {
                        if (i > 0) sb.Append(",");
                        sb.Append(Pt(sp.GetControlPointAt(i)));
                    }
                    sb.Append("]");
                    break;
                case Leader ld:
                    sb.Append(",\"pts\":[");
                    for (int i = 0; i < ld.NumVertices; i++)
                    {
                        if (i > 0) sb.Append(",");
                        sb.Append(Pt(ld.VertexAt(i)));
                    }
                    sb.Append("]");
                    break;
                case DBPoint dp:
                    sb.Append(",\"pos\":").Append(Pt(dp.Position));
                    break;
                default:
                    AppendExtents(sb, e);
                    break;
            }
        }

        // 箭头、粗线是带宽度的多段线：各段等宽就写一个 width，不等宽（箭头）逐段写 [起宽, 止宽]
        static void PolylineWidths(StringBuilder sb, Polyline pl)
        {
            int n = pl.NumberOfVertices;
            bool constant = true;
            for (int i = 0; i < n && constant; i++)
                constant = pl.GetStartWidthAt(i) == pl.GetStartWidthAt(0) && pl.GetEndWidthAt(i) == pl.GetStartWidthAt(0);
            if (n == 0)
                return;
            if (constant)
            {
                sb.Append(",\"width\":").Append(Num(pl.GetStartWidthAt(0)));
                return;
            }
            sb.Append(",\"widths\":[");
            for (int i = 0; i < n; i++)
            {
                if (i > 0) sb.Append(",");
                sb.Append("[").Append(Num(pl.GetStartWidthAt(i))).Append(",").Append(Num(pl.GetEndWidthAt(i))).Append("]");
            }
            sb.Append("]");
        }

        static void DimensionPart(StringBuilder sb, Dimension d)
        {
            sb.Append(",\"kind\":").Append(Str(d.GetType().Name));
            try
            {
                string meas = Num(d.Measurement);
                sb.Append(",\"meas\":").Append(meas);
            }
            catch (System.Exception) { }
            sb.Append(",\"override\":").Append(Str(d.DimensionText ?? ""));
            sb.Append(",\"textpos\":").Append(Pt(d.TextPosition));
            sb.Append(",\"style\":").Append(Str(d.DimensionStyleName ?? ""));
            sb.Append(",\"lfac\":").Append(Num(d.Dimlfac)).Append(",\"dec\":").Append(d.Dimdec);
            sb.Append(",\"zin\":").Append(d.Dimzin).Append(",\"rnd\":").Append(Num(d.Dimrnd));
            sb.Append(",\"post\":").Append(Str(d.Dimpost ?? ""));
            switch (d)
            {
                case RotatedDimension r:
                    sb.Append(",\"x1\":").Append(Pt(r.XLine1Point)).Append(",\"x2\":").Append(Pt(r.XLine2Point));
                    sb.Append(",\"line\":").Append(Pt(r.DimLinePoint)).Append(",\"rot\":").Append(Num(r.Rotation));
                    break;
                case AlignedDimension a:
                    sb.Append(",\"x1\":").Append(Pt(a.XLine1Point)).Append(",\"x2\":").Append(Pt(a.XLine2Point));
                    sb.Append(",\"line\":").Append(Pt(a.DimLinePoint));
                    break;
                case RadialDimension rd:
                    sb.Append(",\"c\":").Append(Pt(rd.Center)).Append(",\"chord\":").Append(Pt(rd.ChordPoint));
                    break;
                case DiametricDimension dd:
                    sb.Append(",\"chord\":").Append(Pt(dd.ChordPoint)).Append(",\"far\":").Append(Pt(dd.FarChordPoint));
                    break;
            }
            // 图面上实际显示的标注文字：标注自己再分解一层，里面的 MTEXT 就是按样式排好的那串字
            try
            {
                using (var inner = new DBObjectCollection())
                {
                    d.Explode(inner);
                    var shown = new List<string>();
                    foreach (DBObject o in inner)
                    {
                        if (o is MText mt) shown.Add(mt.Text);
                        else if (o is DBText dt) shown.Add(dt.TextString);
                        o.Dispose();
                    }
                    if (shown.Count > 0)
                        sb.Append(",\"shown\":").Append(Str(string.Join("\n", shown)));
                }
            }
            catch (System.Exception) { }
        }

        static void Attributes(StringBuilder sb, BlockReference br, Transaction tr)
        {
            try
            {
                int n = 0;
                foreach (object o in br.AttributeCollection)
                {
                    AttributeReference ar = o as AttributeReference;
                    if (ar == null && o is ObjectId id)
                        ar = tr.GetObject(id, OpenMode.ForRead) as AttributeReference;
                    if (ar == null) continue;
                    sb.Append(n++ == 0 ? ",\"attribs\":[" : ",");
                    sb.Append("{\"tag\":").Append(Str(ar.Tag)).Append(",\"text\":").Append(Str(ar.TextString));
                    sb.Append(",\"pos\":").Append(Pt(ar.Position)).Append(",\"h\":").Append(Num(ar.Height));
                    sb.Append(",\"invisible\":").Append(ar.Invisible ? "true" : "false").Append("}");
                }
                if (n > 0) sb.Append("]");
            }
            catch (System.Exception) { }
        }

        static void HatchPart(StringBuilder sb, Hatch h)
        {
            sb.Append(",\"pattern\":").Append(Str(h.PatternName ?? ""));
            try
            {
                string area = Num(h.Area);
                sb.Append(",\"area\":").Append(area);
            }
            catch (System.Exception) { }
            sb.Append(",\"loops\":[");
            for (int i = 0; i < h.NumberOfLoops; i++)
            {
                if (i > 0) sb.Append(",");
                HatchLoop loop = h.GetLoopAt(i);
                sb.Append("[");
                if (loop.IsPolyline)
                {
                    int k = 0;
                    foreach (BulgeVertex bv in loop.Polyline)
                    {
                        if (k++ > 0) sb.Append(",");
                        sb.Append("[").Append(Num(bv.Vertex.X)).Append(",").Append(Num(bv.Vertex.Y)).Append(",").Append(Num(bv.Bulge)).Append("]");
                    }
                }
                else
                {
                    int k = 0;
                    foreach (Curve2d c in loop.Curves)
                    {
                        if (k++ > 0) sb.Append(",");
                        Point2d p = c.StartPoint;
                        sb.Append("[").Append(Num(p.X)).Append(",").Append(Num(p.Y)).Append(",0]");
                    }
                }
                sb.Append("]");
            }
            sb.Append("]");
        }

        static void AppendExtents(StringBuilder sb, Entity e)
        {
            try
            {
                Extents3d x = e.GeometricExtents;
                sb.Append(",\"ext\":[").Append(Num(x.MinPoint.X)).Append(",").Append(Num(x.MinPoint.Y)).Append(",");
                sb.Append(Num(x.MaxPoint.X)).Append(",").Append(Num(x.MaxPoint.Y)).Append("]");
            }
            catch (System.Exception) { }
        }

        static string SymbolName(Transaction tr, ObjectId id)
        {
            if (id.IsNull) return "";
            try { return ((SymbolTableRecord)tr.GetObject(id, OpenMode.ForRead)).Name; }
            catch (System.Exception) { return ""; }
        }

        static string Pt(Point3d p)
        {
            return "[" + Num(p.X) + "," + Num(p.Y) + "]";
        }

        static string Num(double d)
        {
            if (double.IsNaN(d) || double.IsInfinity(d)) return "null";
            return Math.Round(d, 6).ToString("R", CultureInfo.InvariantCulture);
        }

        static string Str(string s)
        {
            if (s == null) return "null";
            var sb = new StringBuilder(s.Length + 2);
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }
    }
}
