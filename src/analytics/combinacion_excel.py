"""Combinar libros de una hoja y una tabla sin cargar las hojas en memoria.

Esta rutina acepta exclusivamente los Excel planos generados por el proyecto.
Conserva los bytes de la hoja del primer libro y remapea estilos y textos de
las siguientes hojas. El llamador valida todas las tablas antes de publicar.
"""
from __future__ import annotations

from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
import posixpath
import re
import shutil
from typing import BinaryIO
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZIP_DEFLATED, ZipFile

from .archivos_excel import abrir_lectura


_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS = {"x": _MAIN}
_SECCIONES = ("numFmts", "fonts", "fills", "borders", "cellStyleXfs",
              "cellXfs", "cellStyles", "dxfs")
_PARTES = {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
           "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet1.xml",
           "xl/worksheets/_rels/sheet1.xml.rels", "xl/theme/theme1.xml",
           "xl/styles.xml", "xl/sharedStrings.xml", "xl/tables/table1.xml",
           "docProps/core.xml", "docProps/app.xml"}
_TOKEN = re.compile(rb'<(?:[^<>"\']|"[^"]*"|\'[^\']*\')*>|[^<]+')
_ATRIBUTO = re.compile(rb'\s([A-Za-z_:][\w:.-]*)="([^"]*)"')
_NO_HOJA_PLANA = {b"f", b"drawing", b"legacyDrawing", b"conditionalFormatting",
                  b"dataValidations", b"hyperlinks", b"extLst", b"AlternateContent"}
_PROHIBIDO = re.compile(rb'<(?:[A-Za-z_]\w*:)?(?:f|drawing|legacyDrawing|conditionalFormatting|dataValidations|hyperlinks|extLst|AlternateContent)(?=[\s/>])')


def _canon(elemento: ET.Element) -> tuple:
    return (elemento.tag, tuple(sorted(elemento.attrib.items())), elemento.text or "",
            tuple(_canon(c) for c in elemento))


def _prefijos(xml: bytes) -> dict[str, str]:
    resultado = {"xml": "http://www.w3.org/XML/1998/namespace"}
    for _, (prefijo, uri) in ET.iterparse(BytesIO(xml), events=("start-ns",)):
        if prefijo in resultado and resultado[prefijo] != uri:
            raise ValueError("Namespaces con prefijos incompatibles.")
        resultado[prefijo] = uri
    return resultado


def _render(elemento: ET.Element, prefijos: dict[str, str]) -> bytes:
    por_uri = {uri: prefijo for prefijo, uri in prefijos.items()}

    def nombre(valor: str) -> str:
        if not valor.startswith("{"):
            return valor
        uri, local = valor[1:].split("}", 1)
        if uri not in por_uri:
            raise ValueError(f"Namespace sin declarar: {uri}")
        prefijo = por_uri[uri]
        return f"{prefijo}:{local}" if prefijo else local

    def convertir(e: ET.Element) -> str:
        tag = nombre(e.tag)
        atributos = "".join(f" {nombre(k)}={quoteattr(v)}" for k, v in e.attrib.items())
        if not len(e) and not e.text:
            return f"<{tag}{atributos}/>"
        # Un CR literal se normaliza a LF al volver a leer XML. La referencia
        # conserva el carácter original, también dentro de textos compartidos.
        contenido = escape(e.text or "").replace("\r", "&#13;")
        contenido += "".join(convertir(c) + escape(c.tail or "").replace("\r", "&#13;") for c in e)
        return f"<{tag}{atributos}>{contenido}</{tag}>"

    return convertir(elemento).encode("utf-8")


def _unir_prefijos(xml: bytes, adicionales: dict[str, str], root: str) -> bytes:
    existentes = _prefijos(xml)
    declaraciones = []
    for prefijo, uri in adicionales.items():
        if prefijo in existentes:
            if existentes[prefijo] != uri:
                raise ValueError("Namespaces incompatibles entre libros.")
        elif prefijo != "xml":
            nombre = f"xmlns:{prefijo}" if prefijo else "xmlns"
            declaraciones.append(f" {nombre}={quoteattr(uri)}".encode())
    if declaraciones:
        patron = re.compile(rb"<" + root.encode() + rb"\b[^>]*>")
        xml, cambios = patron.subn(lambda m: m[0][:-1] + b"".join(declaraciones) + b">", xml, count=1)
        if cambios != 1:
            raise ValueError(f"No se encontró el elemento raíz {root}.")
    return xml


def _seccion(xml: bytes, tag: str, contenido: bytes, cantidad: int) -> bytes:
    etiqueta = tag.encode()
    patron = re.compile(rb"<" + etiqueta + rb"\b[^>]*(?:/>|>.*?</" + etiqueta + rb">)", re.S)
    nuevo = b"<" + etiqueta + b' count="' + str(cantidad).encode() + b'">' + contenido + b"</" + etiqueta + b">"
    coincidencia = patron.search(xml)
    if coincidencia:
        apertura = re.match(rb"<" + etiqueta + rb"\b[^>]*", coincidencia[0])[0].rstrip(b"/")
        apertura = re.sub(rb'\s+count="[^"]*"', b"", apertura)
        nuevo = apertura + b' count="' + str(cantidad).encode() + b'">' + contenido + b"</" + etiqueta + b">"
        return xml[:coincidencia.start()] + nuevo + xml[coincidencia.end():]
    # numFmts es opcional; siempre precede fonts en el esquema OOXML.
    if tag == "numFmts":
        return xml.replace(b"<fonts", nuevo + b"<fonts", 1)
    raise ValueError(f"Falta la sección de estilos {tag}.")


def _mapear_id(atributos: dict[str, str], nombre: str, mapa: dict[int, int]) -> None:
    if nombre in atributos:
        viejo = int(atributos[nombre])
        if viejo not in mapa:
            raise ValueError(f"ID fuera de rango: {nombre}.")
        atributos[nombre] = str(mapa[viejo])


class _Estilos:
    def __init__(self, xml: bytes):
        self.xml = xml
        self.raiz = ET.fromstring(xml)
        self.prefijos = _prefijos(xml)
        desconocidas = {e.tag.rsplit("}", 1)[-1] for e in self.raiz} - set(_SECCIONES) - {"tableStyles", "colors", "extLst"}
        if desconocidas:
            raise ValueError(f"Secciones de estilos no admitidas: {sorted(desconocidas)}")
        self.registros = {s: list(self.raiz.find(f"x:{s}", _NS) or []) for s in _SECCIONES}
        self.indices = {s: {_canon(e): i for i, e in enumerate(self.registros[s])}
                        for s in _SECCIONES if s != "numFmts"}
        self.formatos = {e.get("formatCode"): int(e.get("numFmtId")) for e in self.registros["numFmts"]}

    def agregar(self, xml: bytes) -> dict[str, dict[int, int]]:
        raiz = ET.fromstring(xml)
        adicionales = _prefijos(xml)
        for p, uri in adicionales.items():
            if p in self.prefijos and self.prefijos[p] != uri:
                raise ValueError("Namespaces de estilos incompatibles.")
        self.prefijos.update(adicionales)
        desconocidas = {e.tag.rsplit("}", 1)[-1] for e in raiz} - set(_SECCIONES) - {"tableStyles", "colors", "extLst"}
        if desconocidas:
            raise ValueError("Secciones de estilos adicionales no admitidas.")
        for tag in ("tableStyles", "colors", "extLst"):
            base, fuente = self.raiz.find(f"x:{tag}", _NS), raiz.find(f"x:{tag}", _NS)
            if (base is None) != (fuente is None) or (base is not None and _canon(base) != _canon(fuente)):
                raise ValueError(f"Configuración global de estilos distinta: {tag}.")
        mapas: dict[str, dict[int, int]] = {"numFmts": {i: i for i in range(164)}}
        for formato in raiz.findall("x:numFmts/x:numFmt", _NS):
            antiguo, codigo = int(formato.get("numFmtId")), formato.get("formatCode")
            if codigo not in self.formatos:
                nuevo = max([163, *self.formatos.values()]) + 1
                formato.set("numFmtId", str(nuevo))
                self.registros["numFmts"].append(formato)
                self.formatos[codigo] = nuevo
            mapas["numFmts"][antiguo] = self.formatos[codigo]
        for seccion in _SECCIONES[1:]:
            mapa = mapas[seccion] = {}
            origen = raiz.find(f"x:{seccion}", _NS)
            for indice, elemento in enumerate(list(origen) if origen is not None else []):
                for nodo in elemento.iter():
                    if seccion in ("cellStyleXfs", "cellXfs"):
                        for atributo, dependencia in (("fontId", "fonts"), ("fillId", "fills"),
                                                      ("borderId", "borders"), ("xfId", "cellStyleXfs"),
                                                      ("numFmtId", "numFmts")):
                            _mapear_id(nodo.attrib, atributo, mapas[dependencia])
                    elif seccion == "cellStyles":
                        _mapear_id(nodo.attrib, "xfId", mapas["cellStyleXfs"])
                    elif seccion == "dxfs":
                        _mapear_id(nodo.attrib, "numFmtId", mapas["numFmts"])
                clave = _canon(elemento)
                if clave not in self.indices[seccion]:
                    if seccion == "cellStyles" and any(e.get("name") == elemento.get("name") for e in self.registros[seccion]):
                        raise ValueError("Un estilo con nombre posee otra definición.")
                    self.indices[seccion][clave] = len(self.registros[seccion])
                    self.registros[seccion].append(elemento)
                mapa[indice] = self.indices[seccion][clave]
        return mapas

    def serializar(self) -> bytes:
        xml = _unir_prefijos(self.xml, self.prefijos, "styleSheet")
        for tag in _SECCIONES:
            registros = self.registros[tag]
            if tag == "numFmts" and not registros and self.raiz.find("x:numFmts", _NS) is None:
                continue
            xml = _seccion(xml, tag, b"".join(_render(e, self.prefijos) for e in registros), len(registros))
        return xml


class _Textos:
    def __init__(self, xml: bytes):
        self.xml = xml
        self.raiz = ET.fromstring(xml)
        self.prefijos = _prefijos(xml)
        self.registros = list(self.raiz)
        self.indices = {_canon(e): i for i, e in enumerate(self.registros)}
        self.cantidad = int(self.raiz.get("count", str(len(self.registros))))

    def agregar(self, xml: bytes) -> dict[int, int]:
        raiz = ET.fromstring(xml)
        adicionales = _prefijos(xml)
        for p, uri in adicionales.items():
            if p in self.prefijos and self.prefijos[p] != uri:
                raise ValueError("Namespaces de textos incompatibles.")
        self.prefijos.update(adicionales)
        mapa = {}
        for indice, elemento in enumerate(raiz):
            if elemento.tag != f"{{{_MAIN}}}si":
                raise ValueError("Elemento compartido no contemplado.")
            clave = _canon(elemento)
            if clave not in self.indices:
                self.indices[clave] = len(self.registros)
                self.registros.append(elemento)
            mapa[indice] = self.indices[clave]
        self.cantidad += int(raiz.get("count", str(len(raiz))))
        return mapa

    def serializar(self) -> bytes:
        xml = _unir_prefijos(self.xml, self.prefijos, "sst")
        apertura = re.search(rb"<sst\b[^>]*>", xml)[0]
        apertura = re.sub(rb'\s+(?:count|uniqueCount)="[^"]*"', b"", apertura)
        apertura = apertura[:-1] + f' count="{self.cantidad}" uniqueCount="{len(self.registros)}">'.encode()
        prefijo = xml[:re.search(rb"<sst\b", xml).start()]
        return prefijo + apertura + b"".join(_render(e, self.prefijos) for e in self.registros) + b"</sst>"


def _nombre_tag(token: bytes) -> bytes:
    nombre = re.match(rb"</?([^\s/>]+)", token)
    return nombre[1].split(b":")[-1] if nombre is not None else b""


def _escribir_hoja(entrada: BinaryIO, salida: BinaryIO,
                    mapas: dict[str, dict[int, int]] | None,
                    textos: dict[int, int] | None) -> None:
    if mapas is None:
        # La hoja grande de Colas conserva exactamente sus bytes e índices.
        anterior = b""
        while bloque := entrada.read(1024 * 1024):
            if _PROHIBIDO.search(anterior + bloque):
                raise ValueError("La hoja base contiene características fuera del contrato plano.")
            salida.write(bloque)
            anterior = bloque[-128:]
        return
    pendiente = b""
    celda_texto = captura = False
    valor = []
    while True:
        bloque = entrada.read(1024 * 1024)
        pendiente += bloque
        cursor = 0
        resultado = []
        while cursor < len(pendiente):
            coincidencia = _TOKEN.match(pendiente, cursor)
            if coincidencia is None:
                break
            token = coincidencia[0]
            cursor = coincidencia.end()
            if token.startswith(b"<"):
                nombre = _nombre_tag(token)
                cierre = token.startswith(b"</")
                if not cierre and nombre in _NO_HOJA_PLANA:
                    raise ValueError(f"Característica fuera del contrato de hoja plana: {nombre.decode()}.")
                if mapas is not None:
                    atributos = dict(_ATRIBUTO.findall(token))
                    if nombre in (b"c", b"row", b"col") and not cierre:
                        atributo = b"style" if nombre == b"col" else b"s"
                        if atributo in atributos:
                            antiguo = int(atributos[atributo])
                            if antiguo not in mapas["cellXfs"]:
                                raise ValueError("Estilo de celda/fila/columna fuera de rango.")
                            nuevo = str(mapas["cellXfs"][antiguo]).encode()
                            token = re.sub(rb'(\s' + atributo + rb'=")[^"]*(")',
                                           lambda m: m[1] + nuevo + m[2], token, count=1)
                    if nombre == b"sheetView" and not cierre:
                        token = re.sub(rb'\s+tabSelected="[^"]*"', b' tabSelected="0"', token)
                    if nombre == b"c":
                        celda_texto = not cierre and atributos.get(b"t") == b"s"
                    if nombre == b"v" and celda_texto:
                        if not cierre:
                            captura, valor = True, []
                        else:
                            indice = int(b"".join(valor).strip())
                            if textos is None or indice not in textos:
                                raise ValueError("Texto compartido fuera de rango.")
                            resultado.append(str(textos[indice]).encode())
                            captura = False
                resultado.append(token)
            elif captura:
                valor.append(token)
            else:
                resultado.append(token)
        salida.write(b"".join(resultado))
        pendiente = pendiente[cursor:]
        if not bloque:
            if pendiente or captura:
                raise ValueError("XML de hoja incompleto.")
            break


def _fuente(z: ZipFile, hoja: str, tabla: str) -> None:
    if set(z.namelist()) != _PARTES:
        raise ValueError("El libro contiene partes ajenas al contrato de una tabla plana.")
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    hojas = workbook.findall("x:sheets/x:sheet", _NS)
    if len(hojas) != 1 or hojas[0].get("name") != hoja:
        raise ValueError("La fuente debe contener una sola hoja con el nombre esperado.")
    if workbook.findall("x:definedNames/x:definedName", _NS):
        raise ValueError("No se admiten nombres definidos en estos generadores planos.")
    elemento = ET.fromstring(z.read("xl/tables/table1.xml"))
    if elemento.get("name") != tabla or elemento.get("displayName") != tabla:
        raise ValueError("Nombre de tabla de origen inesperado.")
    relaciones = ET.fromstring(z.read("xl/worksheets/_rels/sheet1.xml.rels"))
    if len(relaciones) != 1 or relaciones[0].get("Type") != _OFFICE_REL + "/table":
        raise ValueError("La hoja sólo puede estar relacionada con su tabla.")


def _tabla(xml: bytes, identificador: int, mapa_dxf: dict[int, int]) -> bytes:
    def reemplazar(m):
        viejo = int(m[2])
        if viejo not in mapa_dxf:
            raise ValueError("Estilo diferencial de tabla fuera de rango.")
        return m[1] + str(mapa_dxf[viejo]).encode() + m[3]
    xml = re.sub(rb'(\s(?:[A-Za-z]*DxfId|dxfId)=")(\d+)(")', reemplazar, xml)
    xml, cantidad = re.subn(rb'(<table\b[^>]*\s)id="\d+"',
                           lambda m: m[1] + f'id="{identificador}"'.encode(), xml, count=1)
    if cantidad != 1:
        raise ValueError("La tabla carece de identificador global.")
    return xml


def combinar_excel(fuentes: list[tuple[Path, str, str, str]], destino: Path) -> None:
    """Escribir un combinado temporal; el llamador publica tras validar firmas."""
    if not fuentes or len({f[2] for f in fuentes}) != len(fuentes) or len({f[3] for f in fuentes}) != len(fuentes):
        raise ValueError("Se requieren hojas y tablas con nombres únicos.")
    destino = Path(destino)
    if destino.resolve() in {Path(f[0]).resolve() for f in fuentes}:
        raise ValueError("El destino no puede sobrescribir una fuente.")
    with ExitStack() as contexto:
        archivos = [contexto.enter_context(abrir_lectura(ruta)) for ruta, _, _, _ in fuentes]
        libros = [contexto.enter_context(ZipFile(a)) for a in archivos]
        for libro, (_, hoja, _, tabla) in zip(libros, fuentes):
            _fuente(libro, hoja, tabla)
        base = libros[0]
        for libro in libros[1:]:
            if libro.read("xl/theme/theme1.xml") != base.read("xl/theme/theme1.xml"):
                raise ValueError("Los temas de los libros deben ser idénticos.")
        sistemas_fechas = []
        for libro in libros:
            propiedades = ET.fromstring(libro.read("xl/workbook.xml")).find("x:workbookPr", _NS)
            sistemas_fechas.append(propiedades is not None and propiedades.get("date1904", "0") in ("1", "true"))
        if len(set(sistemas_fechas)) != 1:
            raise ValueError("Los sistemas de fechas 1900/1904 deben coincidir.")
        estilos, textos = _Estilos(base.read("xl/styles.xml")), _Textos(base.read("xl/sharedStrings.xml"))
        mapas = [None]
        textos_mapas = [None]
        for libro in libros[1:]:
            mapas.append(estilos.agregar(libro.read("xl/styles.xml")))
            textos_mapas.append(textos.agregar(libro.read("xl/sharedStrings.xml")))
        workbook = base.read("xl/workbook.xml")
        # El combinado es un libro nuevo: no hereda punteros de coautoría ni
        # la carpeta temporal cached absPath de los generadores originales.
        workbook = re.sub(rb"<xr:revisionPtr\b[^>]*/>", b"", workbook)
        workbook = re.sub(rb"<mc:AlternateContent\b[^>]*>.*?</mc:AlternateContent>",
                          lambda m: b"" if re.search(rb"<(?:[A-Za-z_]\w*:)?absPath\b", m[0]) else m[0],
                          workbook, flags=re.S)
        raiz = ET.fromstring(workbook)
        sheet = raiz.find("x:sheets/x:sheet", _NS)
        id_base = int(sheet.get("sheetId"))
        nombre_base = fuentes[0][2]
        workbook = re.sub(rb'(<sheet\b[^>]*\s)name="[^"]*"',
                          lambda m: m[1] + b"name=" + quoteattr(nombre_base).encode(), workbook, count=1)
        agregado = b"".join(f'<sheet name={quoteattr(f[2])} sheetId="{id_base+i}" r:id="rIdCombinado{i+1}"/>'.encode()
                             for i, f in enumerate(fuentes[1:], 1))
        workbook = workbook.replace(b"</sheets>", agregado + b"</sheets>", 1)
        rels = base.read("xl/_rels/workbook.xml.rels")
        nuevas_rels = b"".join(f'<Relationship Id="rIdCombinado{i}" Type="{_OFFICE_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'.encode()
                               for i in range(2, len(fuentes)+1))
        rels = rels.replace(b"</Relationships>", nuevas_rels + b"</Relationships>", 1)
        tipos = base.read("[Content_Types].xml")
        nuevas_partes = b"".join((f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                                  f'<Override PartName="/xl/tables/table{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>').encode()
                                 for i in range(2, len(fuentes)+1))
        tipos = tipos.replace(b"</Types>", nuevas_partes + b"</Types>", 1)
        app = base.read("docProps/app.xml")
        titulos = (f'<TitlesOfParts><vt:vector size="{len(fuentes)}" baseType="lpstr">'
                   + "".join(f"<vt:lpstr>{escape(f[2])}</vt:lpstr>" for f in fuentes)
                   + "</vt:vector></TitlesOfParts>").encode()
        app = re.sub(rb"<TitlesOfParts>.*?</TitlesOfParts>", lambda _: titulos, app, count=1, flags=re.S)
        app = re.sub(rb"(<HeadingPairs>.*?<vt:i4>)\d+(</vt:i4>)",
                     lambda m: m[1]+str(len(fuentes)).encode()+m[2], app, count=1, flags=re.S)
        reemplazos = {"xl/workbook.xml": workbook, "xl/_rels/workbook.xml.rels": rels,
                      "[Content_Types].xml": tipos, "xl/styles.xml": estilos.serializar(),
                      "xl/sharedStrings.xml": textos.serializar(), "docProps/app.xml": app}
        id_tabla_base = int(ET.fromstring(base.read("xl/tables/table1.xml")).get("id"))
        with ZipFile(destino, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True) as salida:
            for nombre in base.namelist():
                if nombre in reemplazos:
                    salida.writestr(nombre, reemplazos[nombre])
                elif nombre == "xl/worksheets/sheet1.xml":
                    with base.open(nombre) as entrada, salida.open(nombre, "w", force_zip64=True) as escritura:
                        _escribir_hoja(entrada, escritura, None, None)
                else:
                    with base.open(nombre) as entrada, salida.open(nombre, "w", force_zip64=True) as escritura:
                        shutil.copyfileobj(entrada, escritura, 1024*1024)
            for indice, libro in enumerate(libros[1:], 2):
                with libro.open("xl/worksheets/sheet1.xml") as entrada, salida.open(f"xl/worksheets/sheet{indice}.xml", "w", force_zip64=True) as escritura:
                    _escribir_hoja(entrada, escritura, mapas[indice-1], textos_mapas[indice-1])
                relacion = libro.read("xl/worksheets/_rels/sheet1.xml.rels")
                relacion = re.sub(rb'Target="[^"]*"', f'Target="../tables/table{indice}.xml"'.encode(), relacion, count=1)
                salida.writestr(f"xl/worksheets/_rels/sheet{indice}.xml.rels", relacion)
                tabla = _tabla(libro.read("xl/tables/table1.xml"), id_tabla_base + indice - 1, mapas[indice-1]["dxfs"])
                salida.writestr(f"xl/tables/table{indice}.xml", tabla)
